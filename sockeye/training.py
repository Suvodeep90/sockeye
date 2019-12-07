# Copyright 2017--2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not
# use this file except in compliance with the License. A copy of the License
# is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""
Code for training
"""
from collections import deque
import logging
import os
import pickle
import random
import shutil
import time
import itertools
from typing import Callable, Dict, List, Optional, Iterable, Tuple, Union

import mxnet as mx
from mxnet.contrib import amp
import numpy as np

from .checkpoint_decoder import CheckpointDecoder
from . import constants as C
from . import data_io
from . import horovod_mpi
from . import loss
from . import lr_scheduler
from . import utils
from . import vocab
from . import parallel
from .config import Config
from .model import SockeyeModel
from .optimizers import OptimizerConfig

logger = logging.getLogger(__name__)


class TrainerConfig(Config):
    def __init__(self,
                 output_dir: str,
                 early_stopping_metric: str,
                 max_params_files_to_keep: int,
                 keep_initializations: bool,
                 checkpoint_interval: int,
                 max_num_checkpoint_not_improved: int,
                 checkpoint_improvement_threshold: float,
                 max_checkpoints: Optional[int] = None,
                 min_samples: Optional[int] = None,
                 max_samples: Optional[int] = None,
                 min_updates: Optional[int] = None,
                 max_updates: Optional[int] = None,
                 min_epochs: Optional[int] = None,
                 max_epochs: Optional[int] = None,
                 max_seconds: Optional[int] = None,
                 update_interval: int = 1,
                 stop_training_on_decoder_failure: bool = False) -> None:
        super().__init__()
        self.output_dir = output_dir
        self.early_stopping_metric = early_stopping_metric
        self.max_params_files_to_keep = max_params_files_to_keep
        self.keep_initializations = keep_initializations
        self.checkpoint_interval = checkpoint_interval
        self.max_num_checkpoint_not_improved = max_num_checkpoint_not_improved
        self.checkpoint_improvement_threshold = checkpoint_improvement_threshold
        self.max_checkpoints = max_checkpoints
        self.min_samples = min_samples
        self.max_samples = max_samples
        self.min_updates = min_updates
        self.max_updates = max_updates
        self.min_epochs = min_epochs
        self.max_epochs = max_epochs
        self.max_seconds = max_seconds
        self.update_interval = update_interval
        self.stop_training_on_decoder_failure = stop_training_on_decoder_failure


class TrainState:
    """
    Stores the state an EarlyStoppingTrainer instance.
    """

    __slots__ = ['num_not_improved', 'epoch', 'checkpoint', 'best_checkpoint', 'batches',
                 'updates', 'samples', 'gradient_norm', 'gradients', 'metrics', 'start_tic',
                 '_tic_last_time_elapsed', '_time_elapsed', 'early_stopping_metric',
                 'best_metric', 'best_metric_history', 'best_checkpoint', 'converged', 'diverged']

    def __init__(self, early_stopping_metric: str) -> None:
        self.num_not_improved = 0
        self.epoch = 0
        self.checkpoint = 0
        self.best_checkpoint = 0
        self.batches = 0
        self.updates = 0
        self.samples = 0
        self.gradient_norm = None  # type: Optional[float]
        self.gradients = {}  # type: Dict[str, List[mx.nd.NDArray]]
        # stores dicts of metric names & values for each checkpoint
        self.metrics = []  # type: List[Dict]
        self.start_tic = time.time()
        self._tic_last_time_elapsed = self.start_tic
        self._time_elapsed = 0.0
        self.early_stopping_metric = early_stopping_metric
        self.best_metric = C.METRIC_WORST[early_stopping_metric]
        # List of the last N best metrics, used for threshold-based stopping
        self.best_metric_history = deque([self.best_metric])
        self.best_checkpoint = 0
        self.converged = False
        self.diverged = False

    def save(self, fname: str):
        """
        Saves this training state to fname.
        """
        self.update_time_elapsed()
        with open(fname, "wb") as fp:
            pickle.dump(self, fp)

    @staticmethod
    def load(fname: str) -> 'TrainState':
        """
        Loads a training state from fname.
        """
        with open(fname, "rb") as fp:
            state = pickle.load(fp)
            state._tic_last_time_elapsed = time.time()
            return state

    def update_time_elapsed(self):
        current_time = time.time()
        self._time_elapsed += current_time - self._tic_last_time_elapsed
        self._tic_last_time_elapsed = current_time

    @property
    def time_elapsed(self):
        return self._time_elapsed


class GluonEarlyStoppingTrainer:
    def __init__(self,
                 config: TrainerConfig,
                 optimizer_config: OptimizerConfig,
                 sockeye_model: SockeyeModel,
                 trainer: mx.gluon.Trainer,
                 loss_functions: List[loss.Loss],
                 context: List[mx.context.Context],
                 dtype: str,
                 using_amp: bool = False,
                 custom_metrics_logger: Optional[Callable] = None) -> None:
        self.config = config
        self.optimizer_config = optimizer_config
        self.model = sockeye_model
        self.trainer = trainer
        self.loss_functions = loss_functions
        self.context = context
        self.dtype = dtype
        self.using_amp = using_amp
        self._parallel = parallel.Parallel(len(context) if len(context) > 1 else 0,
                                           ParallelModel(sockeye_model,
                                                         loss_functions,
                                                         trainer,
                                                         using_amp=using_amp))
        self.state = None  # type: Optional[TrainState]
        self._speedometer = Speedometer(frequency=C.MEASURE_SPEED_EVERY, auto_reset=False)
        self._custom_metrics_logger = custom_metrics_logger

    def fit(self,
            train_iter: data_io.BaseParallelSampleIter,
            validation_iter: data_io.BaseParallelSampleIter,
            checkpoint_decoder: Optional[CheckpointDecoder] = None):
        logger.info("Early stopping by optimizing '%s'", self.config.early_stopping_metric)

        if self.config.early_stopping_metric in C.METRICS_REQUIRING_DECODER:
            utils.check_condition(checkpoint_decoder is not None,
                                  "%s requires CheckpointDecoder" % self.config.early_stopping_metric)

        has_bandit = hasattr(train_iter, 'bandit')
        if has_bandit:
            logger.info("Training with bandit curriculum learning with the %s bandit" % train_iter.bandit.name())

        resume_training = os.path.exists(self.training_state_dirname)
        if resume_training:
            logger.info("Found partial training in '%s'. Resuming from saved state.", self.training_state_dirname)
            self._load_training_state(train_iter)
        else:
            self.state = TrainState(self.config.early_stopping_metric)
            self.model.save_config(self.config.output_dir)
            self.model.save_version(self.config.output_dir)
            #~ self._save_training_state(train_iter)
            #self._save_trainer_states(self.best_optimizer_states_fname) # not saving due to deferred initialization
            logger.info("Training started.")

        tic = time.time()

        if self.config.max_checkpoints is not None:
            self.config.max_updates = self.state.updates + self.config.max_checkpoints * self.config.checkpoint_interval
            logger.info("Resetting max_updates to %d + %d * %d = %d in order to implement stopping after (an additional) %d checkpoints.",
                        self.state.updates,
                        self.config.max_checkpoints,
                        self.config.checkpoint_interval,
                        self.config.max_updates,
                        self.config.max_checkpoints)

        while True:
            if self.config.max_epochs is not None and self.state.epoch == self.config.max_epochs:
                logger.info("Maximum # of epochs (%s) reached.", self.config.max_epochs)
                break

            if self.config.max_updates is not None and self.state.updates == self.config.max_updates:
                logger.info("Maximum # of updates (%s) reached.", self.config.max_updates)
                break

            if self.config.max_samples is not None and self.state.samples >= self.config.max_samples:
                logger.info("Maximum # of samples (%s) reached", self.config.max_samples)
                break

            self._step(batch=train_iter.next(), train_iter=train_iter)

            if not train_iter.iter_next():
                self.state.epoch += 1
                train_iter.reset()

            if self.state.updates > 0 and self.state.batches % (
                    self.config.checkpoint_interval * self.config.update_interval) == 0:
                time_cost = time.time() - tic
                self.state.checkpoint += 1

                # (1) save parameters and evaluate on validation data
                self._save_params()

                logger.info("Checkpoint [%d]\tUpdates=%d Epoch=%d Samples=%d Time-cost=%.3f Updates/sec=%.3f",
                            self.state.checkpoint, self.state.updates, self.state.epoch,
                            self.state.samples, time_cost, self.config.checkpoint_interval / time_cost)
                logger.info('Checkpoint [%d]\t%s', self.state.checkpoint,
                            "\t".join("Train-%s" % str(lf.metric) for lf in self.loss_functions))
                # If we have a bandit iterator, log bandit probabilities
                if has_bandit:
                    probs = train_iter.bandit.prob
                    avgrewards = train_iter.bandit.avg
                    str_metrics = list()
                    for idx, (p, r) in enumerate(zip(probs, avgrewards)):
                        str_metrics.append("Train-bandit-probs[%d]=%f" % (idx, p))
                        str_metrics.append("Train-bandit-avg-reward[%d]=%f" % (idx, r))
                    logger.info("Checkpoint [%d]\t%s", self.state.checkpoint, ' '.join(str_metrics))

                safe_custom_metrics_logger(logging_function=self._custom_metrics_logger,
                                           metrics=(lf.metric for lf in self.loss_functions),
                                           global_step=self.state.checkpoint)

                val_metrics = self._evaluate(self.state.checkpoint, validation_iter, checkpoint_decoder)

                mx.nd.waitall()

                has_improved = self._determine_improvement(val_metrics)
                self.state.converged = self._determine_convergence()
                self.state.diverged = self._determine_divergence(val_metrics)
                self._adjust_learning_rate(has_improved)
                if has_improved:
                    self._update_best_params()
                    self._save_trainer_states(self.best_optimizer_states_fname)
                self._save_training_state(train_iter)

                self._write_metrics_file(train_metrics=[l.metric for l in self.loss_functions], val_metrics=val_metrics)
                for lf in self.loss_functions:
                    lf.metric.reset()

                if self.config.max_seconds is not None and self.state.time_elapsed >= self.config.max_seconds:
                    logger.info("Maximum # of seconds (%s) reached. Training ran for %d seconds.",
                                self.config.max_seconds, self.state.time_elapsed)
                    break

                if self.state.converged or self.state.diverged:
                    break

                tic = time.time()

        logger.info("Training finished%s. Best checkpoint: %d. Best validation %s: %.6f",
                    ", can be continued later" if not self.state.converged else "",
                    self.state.best_checkpoint, self.state.early_stopping_metric, self.state.best_metric)

        # Always keep the training state to allow continuing training with
        # different stopping criteria
        self._cleanup(keep_training_state=True)
        return self.state

    def _forward_backward(self, batch: data_io.Batch, call_backward: bool = True):
        """
        Performs forward-backward pass on a batch in data-parallel mode.

        :param batch: Current data batch.
        :return: List loss outputs (tuple of loss value and number of samples) for each loss function.
        """
        # split batch into shards
        batch = batch.split_and_load(ctx=self.context)

        # send sharded inputs to the backend
        for inputs, labels in batch.shards():
            self._parallel.put((inputs, labels), call_backward)

        # get outputs from parallel requests to the backend. Each shard output contains a list of tuples, one for each
        # loss function of the form: (loss_value, num_samples).
        sharded_outputs = [self._parallel.get() for _ in range(len(self.context))]

        # repack outputs into a list of loss_values (length = number of shards) for each loss function
        sharded_outputs_per_loss_function = list(zip(*sharded_outputs))

        # sum loss values (on the cpu) and number of samples for each loss function
        output_per_loss_function = [
            tuple(mx.nd.add_n(*(s.as_in_context(mx.cpu()) for s in shard)) for shard in zip(*outs)) for outs in
            sharded_outputs_per_loss_function]
        return output_per_loss_function

    def _step(self, batch: data_io.Batch, train_iter: data_io.BaseParallelSampleIter):
        self.state.batches += 1
        loss_outputs = self._forward_backward(batch)
        if self.config.update_interval == 1 or self.state.batches % self.config.update_interval == 0:
            # `step` rescales the gradients for the number of batches in this update.
            self.trainer.step(batch_size=self.config.update_interval)

            # let the bandit know about the gradient norm
            if hasattr(train_iter, 'update'):
                reward = data_io.bandit_reward(self, batch, train_iter.bandit,
                                               loss_outputs = loss_outputs)
                perplexity, num_samples = None, None
                for loss_func, (loss_val, num) in zip(self.loss_functions, loss_outputs):
                    if loss_func.name == C.CROSS_ENTROPY:
                        perplexity, num_samples = loss_val, num
                        break
                if reward is not None:  # 'none' bandit can return None
                    train_iter.update(reward, perplexity, num_samples)

            if self.config.update_interval > 1:
                # Multi-batch updates sum gradients for each batch instead of
                # overwriting, so gradients must be manually zeroed after each
                # update.
                self.model.collect_params().zero_grad()
            self.state.updates += 1

        self.state.samples += batch.samples
        for loss_func, (loss_value, num_samples) in zip(self.loss_functions, loss_outputs):
            loss_func.metric.update(loss_value.asscalar(), num_samples.asscalar())
        self._speedometer(self.state.epoch, self.state.batches,
                          self.state.updates, batch.samples, batch.tokens, (lf.metric for lf in self.loss_functions))

    def _evaluate(self, checkpoint: int, data_iter,
                  checkpoint_decoder: Optional[CheckpointDecoder]) -> List[loss.LossMetric]:
        """
        Computes loss(es) on validation data and returns their metrics.
        :param data_iter: Validation data iterator.
        :return: List of validation metrics, same order as self.loss_functions.
        """
        data_iter.reset()
        val_metrics = [lf.create_metric() for lf in self.loss_functions]
        has_bandit = hasattr(data_iter, 'cluster_iters')
        if has_bandit:
            cluster_metrics = [[lf.create_metric(suffix="[%d]" % idx) \
                                for lf in self.loss_functions] for idx in range(len(data_iter.cluster_iters))]
        sharded_loss_outputs = []
        for batch in data_iter:
            batch = batch.split_and_load(ctx=self.context)
            for inputs, labels in batch.shards():
                outputs = self.model(*inputs)  # type: Dict[str, mx.nd.NDArray]
                loss_outputs = [loss_function(outputs, labels) for loss_function in self.loss_functions]
                sharded_loss_outputs.append(loss_outputs)

            # repack outputs into a list of loss_values (length = number of shards) for each loss function
            sharded_loss_outputs_per_loss_function = list(zip(*sharded_loss_outputs))
            # sum loss values (on the cpu) and number of samples for each loss function
            output_per_loss_function = [tuple(mx.nd.add_n(*(s.as_in_context(mx.cpu()) for s in shard))
                                        for shard in zip(*outs)) for outs in sharded_loss_outputs_per_loss_function]
            # update validation metrics for batch
            for loss_metric, (loss_value, num_samples) in zip(val_metrics, output_per_loss_function):
                loss_metric.update(loss_value.asscalar(), num_samples.asscalar())
            # update validation metrics for bandit for the selected cluster
            if has_bandit:
                for loss_metric, (loss_value, num_samples) in zip(cluster_metrics[data_iter.action], output_per_loss_function):
                    loss_metric.update(loss_value.asscalar(), num_samples.asscalar())
        
        # copy cluster_metrics into val_metrics:
        if has_bandit:
            val_metrics.extend(itertools.chain.from_iterable(cluster_metrics))

        # Optionally run the checkpoint decoder
        if checkpoint_decoder is not None:
            output_name = os.path.join(self.config.output_dir, C.DECODE_OUT_NAME % checkpoint)
            decoder_metrics = checkpoint_decoder.decode_and_evaluate(output_name=output_name, shard_into_clusters=has_bandit)
            for metric_name, metric_value in decoder_metrics.items():
                assert metric_name not in val_metrics, "Duplicate validation metric %s" % metric_name
                metric = loss.LossMetric(name=metric_name)
                metric.update(metric_value, num_samples=1)
                val_metrics.append(metric)

        logger.info('Checkpoint [%d]\t%s',
                    self.state.checkpoint, " ".join("Validation-%s" % str(lm) for lm in val_metrics))

        safe_custom_metrics_logger(logging_function=self._custom_metrics_logger,
                                   metrics=val_metrics,
                                   global_step=self.state.checkpoint)

        return val_metrics

    def _determine_improvement(self, val_metrics: List[loss.LossMetric]) -> bool:
        """
        Determines whether early stopping metric on validation data improved and updates best value and checkpoint in
        the state.
        :param val_metrics: Validation metrics.
        :return: Whether model has improved on held-out data since last checkpoint.
        """
        value = None
        value_is_better = False
        for val_metric in val_metrics:
            if val_metric.name == self.config.early_stopping_metric:
                value = val_metric.get()
                # When using Horovod, the primary worker makes an authoritative
                # check of whether metric value has improved and broadcasts the
                # result to secondary workers.  Non-determinism in the order of
                # GPU operations can lead to slight numeric variation across
                # workers, causing potential desync if each worker makes its own
                # check for key training decisions (reducing learning rate,
                # early stopping, etc.).
                if not horovod_mpi.using_horovod() or horovod_mpi.hvd.rank() == 0:
                    # Horovod primary worker or not using Horovod: make
                    # authoritative metric check.
                    value_is_better = utils.metric_value_is_better(value,
                                                                   self.state.best_metric,
                                                                   self.config.early_stopping_metric)
                if horovod_mpi.using_horovod():
                    # Broadcast result across workers.
                    value_is_better = horovod_mpi.MPI.COMM_WORLD.bcast(value_is_better, root=0)
                if value_is_better:
                    logger.info("Validation-%s improved to %f (delta=%f).", self.config.early_stopping_metric,
                                value, abs(value - self.state.best_metric))
                    self.state.best_metric = value
                    self.state.best_checkpoint = self.state.checkpoint
                    self.state.num_not_improved = 0
        assert value is not None, "Early stopping metric %s not found in validation metrics." % self.config.early_stopping_metric
        if not value_is_better:
            self.state.num_not_improved += 1
            logger.info("Validation-%s has not improved for %d checkpoints, best so far: %f",
                        self.config.early_stopping_metric, self.state.num_not_improved, self.state.best_metric)
        # Update best metric history
        self.state.best_metric_history.append(self.state.best_metric)
        if (self.config.max_num_checkpoint_not_improved is not None
                and len(self.state.best_metric_history) > self.config.max_num_checkpoint_not_improved + 1):
            self.state.best_metric_history.popleft()

        return value_is_better

    def _determine_convergence(self) -> bool:
        """
        True if model has converged w.r.t early stopping criteria (patience).
        Order: first check required minimums (samples, updates, epochs), then
        check early stopping criteria (checkpoints not improved).
        """
        if self.config.min_samples is not None and self.state.samples < self.config.min_samples:
            logger.info("Minimum number of samples (%d) not reached yet: %d",
                        self.config.min_samples, self.state.samples)
            return False

        if self.config.min_updates is not None and self.state.updates < self.config.min_updates:
            logger.info("Minimum number of updates (%d) not reached yet: %d",
                        self.config.min_updates, self.state.updates)
            return False

        if self.config.min_epochs is not None and self.state.epoch < self.config.min_epochs:
            logger.info("Minimum number of epochs (%d) not reached yet: %d",
                        self.config.min_epochs, self.state.epoch)
            return False

        if (self.config.max_num_checkpoint_not_improved is not None
                and 0 <= self.config.max_num_checkpoint_not_improved
                and self.state.checkpoint >= self.config.max_num_checkpoint_not_improved):
            # When using Horovod, the primary worker makes the authoritative
            # calculation of improvement over the window for evaluating stopping
            window_improvement = 0.
            if not horovod_mpi.using_horovod() or horovod_mpi.hvd.rank() == 0:
                window_improvement = abs(self.state.best_metric - self.state.best_metric_history[0])
            if horovod_mpi.using_horovod():
                window_improvement = horovod_mpi.MPI.COMM_WORLD.bcast(window_improvement, root=0)

            # <= to correctly handle threshold == 0
            if window_improvement <= self.config.checkpoint_improvement_threshold:
                logger.info("Maximum number of not improved checkpoints reached: "
                            "improvement %f <= %f over %d checkpoints", window_improvement,
                            self.config.checkpoint_improvement_threshold, self.config.max_num_checkpoint_not_improved)
                return True
            else:
                logger.info("Sufficient improvement to continue: %f > %f over %d checkpoints", window_improvement,
                            self.config.checkpoint_improvement_threshold, self.config.max_num_checkpoint_not_improved)

        return False

    def _determine_divergence(self, val_metrics: List[loss.LossMetric]) -> bool:
        """
        True if last perplexity is infinite or >2*target_vocab_size.
        """
        # (5) detect divergence with respect to the perplexity value at the last checkpoint
        last_ppl = float('nan')
        for metric in val_metrics:
            if metric.name == C.PERPLEXITY:
                last_ppl = metric.get()
                break
        # using a double of uniform distribution's value as a threshold
        if not np.isfinite(last_ppl) or last_ppl > 2 * self.model.config.vocab_target_size:
            logger.warning("Model optimization diverged. Last checkpoint's perplexity: %f", last_ppl)
            return True
        return False

    def _adjust_learning_rate(self, has_improved: bool):
        """
        Adjusts the optimizer learning rate if required.
        """
        scheduler = self.trainer.optimizer.lr_scheduler
        if scheduler is not None:
            if issubclass(type(scheduler), lr_scheduler.AdaptiveLearningRateScheduler):
                lr_adjusted = scheduler.new_evaluation_result(has_improved)  # type: ignore
            else:
                lr_adjusted = False
            if lr_adjusted and not has_improved:
                logger.info("Loading model parameters and optimizer states from best checkpoint: %d",
                            self.state.best_checkpoint)
                adjusted_lr = self.trainer.optimizer.lr_scheduler.lr
                # trainer.load_states also reloads the parameters
                self._load_trainer_states(self.best_optimizer_states_fname)
                # state loading replaces the lr_scheduler instance which then contains the old learning rate,
                # overwriting here. TODO: make this better...
                self.trainer.optimizer.lr_scheduler.lr = adjusted_lr

    def _write_metrics_file(self, train_metrics: List[loss.LossMetric], val_metrics: List[loss.LossMetric]):
        """
        Updates metrics for current checkpoint.
        Writes all metrics to the metrics file and optionally logs to tensorboard.
        """
        data = {"epoch": self.state.epoch,
                "learning-rate": self.trainer.optimizer.lr_scheduler.lr,
                "gradient-norm": self.state.gradient_norm,
                "time-elapsed": self.state.time_elapsed}
        gpu_memory_usage = utils.get_gpu_memory_usage(self.context)
        data['used-gpu-memory'] = sum(v[0] for v in gpu_memory_usage.values())
        data['converged'] = self.state.converged
        data['diverged'] = self.state.diverged

        for metric in train_metrics:
            data["%s-train" % metric.name] = metric.get()
        for metric in val_metrics:
            data["%s-val" % metric.name] = metric.get()

        self.state.metrics.append(data)
        utils.write_metrics_file(self.state.metrics, self.metrics_fname)

        # TODO: Tensorboard logging
        # tf_metrics = data.copy()
        # tf_metrics.update({"%s_grad" % n: v for n, v in self.state.gradients.items()})
        # tf_metrics.update(self.model.params)
        #self.tflogger.log_metrics(metrics=tf_metrics, checkpoint=self.state.checkpoint)

    def _update_best_params(self):
        """
        Updates the params.best link to the latest best parameter file.
        """
        actual_best_params_fname = C.PARAMS_NAME % self.state.best_checkpoint
        if os.path.lexists(self.best_params_fname):
            os.remove(self.best_params_fname)
        os.symlink(actual_best_params_fname, self.best_params_fname)
        logger.info("'%s' now points to '%s'", self.best_params_fname, actual_best_params_fname)

    def _save_params(self):
        """
        Saves model parameters at current checkpoint and optionally cleans up older parameter files to save disk space.
        """
        self.model.save_parameters(self.current_params_fname)
        utils.cleanup_params_files(self.config.output_dir, self.config.max_params_files_to_keep, self.state.checkpoint,
                                   self.state.best_checkpoint, self.config.keep_initializations)

    def _save_trainer_states(self, fname):
        trainer_save_states_no_dump_optimizer(self.trainer, fname)
        logger.info('Saved optimizer states to "%s"', fname)

    def _load_trainer_states(self, fname):
        self.trainer.load_states(fname)
        logger.info('Loaded optimizer states from "%s"', fname)

    def _save_training_state(self, train_iter: data_io.BaseParallelSampleIter):
        """
        Saves current training state.
        """
        # Create temporary directory for storing the state of the optimization process
        training_state_dirname = os.path.join(self.config.output_dir, C.TRAINING_STATE_TEMP_DIRNAME)
        if not os.path.exists(training_state_dirname):
            os.mkdir(training_state_dirname)

        # (1) Parameters: link current file
        params_base_fname = C.PARAMS_NAME % self.state.checkpoint
        params_file = os.path.join(training_state_dirname, C.TRAINING_STATE_PARAMS_NAME)
        if os.path.exists(params_file):
            os.unlink(params_file)
        os.symlink(os.path.join("..", params_base_fname), params_file)

        # (2) Optimizer states
        opt_state_fname = os.path.join(training_state_dirname, C.OPT_STATES_LAST)
        self._save_trainer_states(opt_state_fname)

        # (3) Data iterator
        train_iter.save_state(os.path.join(training_state_dirname, C.BUCKET_ITER_STATE_NAME))

        # (4) Random generators
        # RNG states: python's random and np.random provide functions for
        # storing the state, mxnet does not, but inside our code mxnet's RNG is
        # not used AFAIK
        with open(os.path.join(training_state_dirname, C.RNG_STATE_NAME), "wb") as fp:
            pickle.dump(random.getstate(), fp)
            pickle.dump(np.random.get_state(), fp)

        # (5) Training state
        self.state.save(os.path.join(training_state_dirname, C.TRAINING_STATE_NAME))

        # (6) AMP loss scaler state
        if self.using_amp:
            with open(os.path.join(training_state_dirname, C.AMP_LOSS_SCALER_STATE_NAME), "wb") as fp:
                pickle.dump([self.trainer._amp_loss_scaler._loss_scale,
                             self.trainer._amp_loss_scaler._next_loss_scale,
                             self.trainer._amp_loss_scaler._unskipped], fp)

        # First we rename the existing directory to minimize the risk of state
        # loss if the process is aborted during deletion (which will be slower
        # than directory renaming)
        delete_training_state_dirname = os.path.join(self.config.output_dir, C.TRAINING_STATE_TEMP_DELETENAME)
        if os.path.exists(self.training_state_dirname):
            os.rename(self.training_state_dirname, delete_training_state_dirname)
        os.rename(training_state_dirname, self.training_state_dirname)
        if os.path.exists(delete_training_state_dirname):
            try:
                shutil.rmtree(delete_training_state_dirname)
            except FileNotFoundError:
                # This can be occur on file systems with higher latency, such as
                # distributed file systems.  While repeated occurrences of this
                # warning may indicate a problem, seeing one or two warnings
                # during training is usually fine.
                logger.warning('Directory has already been removed: %s', delete_training_state_dirname)

    def _load_training_state(self, train_iter: data_io.BaseParallelSampleIter):
        """
        Loads the full training state from disk.
        :param train_iter: training data iterator.
        """
        # (1) Parameters
        params_fname = os.path.join(self.training_state_dirname, C.TRAINING_STATE_PARAMS_NAME)
        self.model.load_parameters(params_fname, ctx=self.context, allow_missing=False, ignore_extra=False)

        # (2) Optimizer states
        opt_state_fname = os.path.join(self.training_state_dirname, C.OPT_STATES_LAST)
        self._load_trainer_states(opt_state_fname)

        # (3) Data Iterator
        train_iter.load_state(os.path.join(self.training_state_dirname, C.BUCKET_ITER_STATE_NAME))

        # (4) Random generators
        # RNG states: python's random and np.random provide functions for
        # storing the state, mxnet does not, but inside our code mxnet's RNG is
        # not used AFAIK
        with open(os.path.join(self.training_state_dirname, C.RNG_STATE_NAME), "rb") as fp:
            random.setstate(pickle.load(fp))
            np.random.set_state(pickle.load(fp))

        # (5) Training state
        self.state = TrainState.load(os.path.join(self.training_state_dirname, C.TRAINING_STATE_NAME))

        # (6) AMP loss scaler state
        if self.using_amp:
            # Load loss scaler state
            with open(os.path.join(self.training_state_dirname, C.AMP_LOSS_SCALER_STATE_NAME), "rb") as fp:
                (self.trainer._amp_loss_scaler._loss_scale,
                 self.trainer._amp_loss_scaler._next_loss_scale,
                 self.trainer._amp_loss_scaler._unskipped) = pickle.load(fp)

    def _cleanup(self, keep_training_state=False):
        """
        Cleans parameter files, training state directory and waits for remaining decoding processes.
        """
        utils.cleanup_params_files(self.config.output_dir, self.config.max_params_files_to_keep,
                                   self.state.checkpoint, self.state.best_checkpoint, self.config.keep_initializations)
        # if process_manager is not None:
        #     result = process_manager.collect_results()
        #     if result is not None:
        #         decoded_checkpoint, decoder_metrics = result
        #         self.state.metrics[decoded_checkpoint - 1].update(decoder_metrics)
        #         self.tflogger.log_metrics(decoder_metrics, decoded_checkpoint)
        #         utils.write_metrics_file(self.state.metrics, self.metrics_fname)
        #         self.state.save(os.path.join(self.training_state_dirname, C.TRAINING_STATE_NAME))

        if not keep_training_state:
            if os.path.exists(self.training_state_dirname):
                shutil.rmtree(self.training_state_dirname)
            if os.path.exists(self.best_optimizer_states_fname):
                os.remove(self.best_optimizer_states_fname)

    @property
    def metrics_fname(self) -> str:
        return os.path.join(self.config.output_dir, C.METRICS_NAME)

    @property
    def current_params_fname(self) -> str:
        return os.path.join(self.config.output_dir, C.PARAMS_NAME % self.state.checkpoint)

    @property
    def best_params_fname(self) -> str:
        return os.path.join(self.config.output_dir, C.PARAMS_BEST_NAME)

    @property
    def training_state_dirname(self) -> str:
        return os.path.join(self.config.output_dir, C.TRAINING_STATE_DIRNAME)

    @property
    def best_optimizer_states_fname(self) -> str:
        return os.path.join(self.config.output_dir, C.OPT_STATES_BEST)


class ParallelModel(parallel.Parallelizable):

    def __init__(self,
                 model: Callable,
                 loss_functions: List[loss.Loss],
                 trainer: mx.gluon.Trainer,
                 using_amp: bool = False) -> None:
        self.model = model
        self.loss_functions = loss_functions
        self.trainer = trainer
        self.using_amp = using_amp

    def forward_backward(self, shard: Tuple, call_backward: bool = True) -> List[Tuple[mx.nd.NDArray, mx.nd.NDArray]]:
        """
        Applies forward-backward pass for a single shard of a batch (data-parallel training).
        """
        inputs, labels = shard

        def get_loss_outputs():
            outputs = self.model(*inputs)  # type: Dict[str, mx.nd.NDArray]
            loss_outputs = [loss_function(outputs, labels) for loss_function in self.loss_functions]
            return loss_outputs

        if not call_backward:
            loss_outputs = get_loss_outputs()
            return loss_outputs

        with mx.autograd.record():
            loss_outputs = get_loss_outputs()
            loss_values = (v for v, _ in loss_outputs)
            sum_losses = mx.nd.add_n(*loss_values)
            if self.using_amp:
                # AMP applies dynamic loss scaling to the losses (scale up) and
                # the Trainer (scale down).
                with amp.scale_loss(sum_losses, self.trainer) as scaled_loss:
                    mx.autograd.backward(scaled_loss)
            else:
                # backward on the sum of losses, weights are defined in the loss blocks themselves.
                sum_losses.backward()
        return loss_outputs


class TensorboardLogger:
    """
    Thin wrapper for MXBoard API to log training events.
    Flushes logging events to disk every 60 seconds.

    :param logdir: Directory to write Tensorboard event files to.
    :param source_vocab: Optional source vocabulary to log source embeddings.
    :param target_vocab: Optional target vocabulary to log target and output embeddings.
    """

    def __init__(self,
                 logdir: str,
                 source_vocab: Optional[vocab.Vocab] = None,
                 target_vocab: Optional[vocab.Vocab] = None) -> None:
        self.logdir = logdir
        self.source_labels = vocab.get_ordered_tokens_from_vocab(source_vocab) if source_vocab is not None else None
        self.target_labels = vocab.get_ordered_tokens_from_vocab(target_vocab) if target_vocab is not None else None
        try:
            import mxboard
            logger.info("Logging training events for Tensorboard at '%s'", self.logdir)
            self.sw = mxboard.SummaryWriter(logdir=self.logdir, flush_secs=60, verbose=False)
        except ImportError:
            logger.info("mxboard not found. Consider 'pip install mxboard' to log events to Tensorboard.")
            self.sw = None

    def log_metrics(self, metrics: Dict[str, Union[float, int, mx.nd.NDArray]], checkpoint: int):
        if self.sw is None:
            return

        for name, value in metrics.items():
            if isinstance(value, mx.nd.NDArray):
                if mx.nd.contrib.isfinite(value).sum().asscalar() == value.size:
                    self.sw.add_histogram(tag=name, values=value, bins=100, global_step=checkpoint)
                else:
                    logger.warning("Histogram of %s not logged to tensorboard because of infinite data.")
            else:
                self.sw.add_scalar(tag=name, value=value, global_step=checkpoint)

    def log_graph(self, symbol: mx.sym.Symbol):
        if self.sw is None:
            return
        self.sw.add_graph(symbol)

    def log_source_embedding(self, embedding: mx.nd.NDArray, checkpoint: int):
        if self.sw is None or self.source_labels is None:
            return
        self.sw.add_embedding(tag="source", embedding=embedding, labels=self.source_labels, global_step=checkpoint)

    def log_target_embedding(self, embedding: mx.nd.NDArray, checkpoint: int):
        if self.sw is None or self.target_labels is None:
            return
        self.sw.add_embedding(tag="target", embedding=embedding, labels=self.target_labels, global_step=checkpoint)

    def log_output_embedding(self, embedding: mx.nd.NDArray, checkpoint: int):
        if self.sw is None or self.target_labels is None:
            return
        self.sw.add_embedding(tag="output", embedding=embedding, labels=self.target_labels, global_step=checkpoint)


class Speedometer:
    """
    Custom Speedometer to log samples and words per second.
    """

    def __init__(self, frequency: int = 50, auto_reset: bool = True) -> None:
        self.frequency = frequency
        self.init = False
        self.tic = 0.0
        self.last_count = 0
        self.auto_reset = auto_reset
        self.samples = 0
        self.tokens = 0
        self.msg = 'Epoch[%d] Batch [%d]\tSpeed: %.2f samples/sec %.2f tokens/sec %.2f updates/sec'

    def __call__(self, epoch: int, batches: int, updates: int, samples: int,
                 tokens: int, metrics: Optional[Iterable[loss.LossMetric]] = None):
        count = batches
        if self.last_count > count:
            self.init = False
        self.last_count = count
        self.samples += samples
        self.tokens += tokens

        if self.init:
            if count % self.frequency == 0:
                toc = (time.time() - self.tic)
                update_interval = batches / updates
                updates_per_sec = self.frequency / update_interval / toc
                samples_per_sec = self.samples / toc
                tokens_per_sec = self.tokens / toc
                self.samples = 0
                self.tokens = 0

                if metrics is not None:
                    metric_values = []  # type: List[Tuple[str, float]]
                    for metric in metrics:
                        metric_values.append((metric.name, metric.get()))
                        if self.auto_reset:
                            metric.reset()
                    logger.info(self.msg + '\t%s=%f' * len(metric_values),
                                epoch, count, samples_per_sec, tokens_per_sec, updates_per_sec, *sum(metric_values, ()))

                else:
                    logger.info(self.msg, epoch, count, samples_per_sec, tokens_per_sec, updates_per_sec)

                self.tic = time.time()
        else:
            self.init = True
            self.tic = time.time()


def safe_custom_metrics_logger(logging_function: Callable,
                               metrics: Iterable[loss.LossMetric],
                               global_step: int = None):
    """
    A thin wrapper for calling a custom metrics logging function, if supplied. As it uses an external function,
    it should never throw an exception. If there is no logging_function supplied, the function does nothing.
    :param logging_function: The function supplied by a caller of sockeye.train
    :param metrics: A list of LossMetrics.
    :param global_step: Optional argument, which can be used e.g. by Tensorboard.
    """
    if logging_function is None:
        return
    try:
        logging_function({m.name: m.get() for m in metrics}, global_step)
    except Exception as e:
        logging.warning("Didn't use custom metrics logger, exception '{}' occured".format(str(e)))


def trainer_save_states_no_dump_optimizer(trainer: mx.gluon.Trainer, fname: str):
    """
    Otherwise exact copy of `Trainer.save_states` that does not include a
    pickled optimizer instance as part of the state.  This is compatible with
    the standard `Trainer.load_states`, which will handle a state file with no
    optimizer instance (any statements involving `self._optimizer` become
    no-ops).  This is especially important when using AMP, which patches the
    optimizer at runtime with references to a specific loss scaler instance.
    Loading a stale optimizer instance causes errors.
    """
    assert trainer._optimizer is not None

    if not trainer._kv_initialized:
        trainer._init_kvstore()
    if trainer._params_to_init:
        trainer._init_params()

    if trainer._update_on_kvstore:
        assert not trainer._params_to_init, "Cannot save trainer states when some " \
                                            "parameters are not yet initialized in kvstore."
        trainer._kvstore.save_optimizer_states(fname, dump_optimizer=False)
    else:
        with open(fname, 'wb') as fout:
            fout.write(trainer._updaters[0].get_states(dump_optimizer=False))
