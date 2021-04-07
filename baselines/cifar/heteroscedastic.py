# coding=utf-8
# Copyright 2021 The Uncertainty Baselines Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Wide ResNet 28-10 on CIFAR-10/100 trained with maximum likelihood.

Hyperparameters differ slightly from the original paper's code
(https://github.com/szagoruyko/wide-residual-networks) as TensorFlow uses, for
example, l2 instead of weight decay, and a different parameterization for SGD's
momentum.
"""

import os
import time
from absl import app
from absl import flags
from absl import logging

import tensorflow as tf
import tensorflow_datasets as tfds
import uncertainty_baselines as ub
import utils  # local file import
import uncertainty_metrics as um

flags.DEFINE_integer('seed', 42, 'Random seed.')
flags.DEFINE_integer('per_core_batch_size', 64, 'Batch size per TPU core/GPU.')
flags.DEFINE_float('base_learning_rate', 0.1,
                   'Base learning rate when total batch size is 128. It is '
                   'scaled by the ratio of the total batch size to 128.')
flags.DEFINE_integer('lr_warmup_epochs', 1,
                     'Number of epochs for a linear warmup to the initial '
                     'learning rate. Use 0 to do no warmup.')
flags.DEFINE_float('lr_decay_ratio', 0.2, 'Amount to decay learning rate.')
flags.DEFINE_list('lr_decay_epochs', ['60', '120', '160'],
                  'Epochs to decay learning rate by.')
flags.DEFINE_float(
    'train_proportion', 1.,
    'Only a fraction (between 0 and 1) of the train set is used for training. '
    'The remainder can be used for validation.')
flags.register_validator('train_proportion',
                         lambda tp: tp > 0.0 and tp <= 1.0,
                         message='--train_proportion must be in (0, 1].')

flags.DEFINE_float('l2', 2e-4, 'L2 regularization coefficient.')
flags.DEFINE_float('label_smoothing', 0., 'Label smoothing parameter in [0,1].')
flags.register_validator('label_smoothing',
                         lambda ls: ls >= 0.0 and ls <= 1.0,
                         message='--label_smoothing must be in [0, 1].')

# Fine-grained specification of the hyperparameters (used when FLAGS.l2 is None)
flags.DEFINE_float('bn_l2', None, 'L2 reg. coefficient for batch-norm layers.')
flags.DEFINE_float('input_conv_l2', None,
                   'L2 reg. coefficient for the input conv layer.')
flags.DEFINE_float('group_1_conv_l2', None,
                   'L2 reg. coefficient for the 1st group of conv layers.')
flags.DEFINE_float('group_2_conv_l2', None,
                   'L2 reg. coefficient for the 2nd group of conv layers.')
flags.DEFINE_float('group_3_conv_l2', None,
                   'L2 reg. coefficient for the 3rd group of conv layers.')
flags.DEFINE_float('dense_kernel_l2', None,
                   'L2 reg. coefficient for the kernel of the dense layer.')
flags.DEFINE_float('dense_bias_l2', None,
                   'L2 reg. coefficient for the bias of the dense layer.')
flags.DEFINE_enum('dataset', 'cifar10',
                  enum_values=['cifar10', 'cifar100'],
                  help='Dataset.')
flags.DEFINE_string('cifar100_c_path', None,
                    'Path to the TFRecords files for CIFAR-100-C. Only valid '
                    '(and required) if dataset is cifar100 and corruptions.')
flags.DEFINE_string('data_dir', None,
                    'data_dir to be used for tfds dataset construction.'
                    'It is required when training with cloud TPUs')
flags.DEFINE_integer('corruptions_interval', -1,
                     'Number of epochs between evaluating on the corrupted '
                     'test data. Use -1 to never evaluate.')
flags.DEFINE_integer('checkpoint_interval', 25,
                     'Number of epochs between saving checkpoints. Use -1 to '
                     'never save checkpoints.')
flags.DEFINE_integer('num_bins', 15, 'Number of bins for ECE.')
flags.DEFINE_string('output_dir', '/tmp/cifar', 'Output directory.')
flags.DEFINE_integer('train_epochs', 200, 'Number of training epochs.')
flags.DEFINE_bool('collect_profile', False,
                  'Whether to trace a profile with tensorboard')

# Accelerator flags.
flags.DEFINE_bool('use_gpu', False, 'Whether to run on GPU or otherwise TPU.')
flags.DEFINE_integer('num_cores', 8, 'Number of TPU cores or number of GPUs.')
flags.DEFINE_string('tpu', None,
                    'Name of the TPU. Only used if use_gpu is False.')

# Heteroscedastic flags.
flags.DEFINE_integer('num_factors', 6,
                     'Num factors to approximate full rank covariance matrix.')
flags.DEFINE_float('temperature', 1.3,
                   'Temperature for heteroscedastic head.')
flags.DEFINE_integer('num_mc_samples', 10000,
                     'Num MC samples for heteroscedastic layer.')

FLAGS = flags.FLAGS


def _extract_hyperparameter_dictionary():
  """Create the dictionary of hyperparameters from FLAGS."""
  flags_as_dict = FLAGS.flag_values_dict()
  hp_keys = ub.models.models.wide_resnet.HP_KEYS
  hps = {k: flags_as_dict[k] for k in hp_keys}
  return hps


def main(argv):
  fmt = '[%(filename)s:%(lineno)s] %(message)s'
  formatter = logging.PythonFormatter(fmt)
  logging.get_absl_handler().setFormatter(formatter)
  del argv  # unused arg

  tf.io.gfile.makedirs(FLAGS.output_dir)
  logging.info('Saving checkpoints at %s', FLAGS.output_dir)
  tf.random.set_seed(FLAGS.seed)

  data_dir = None
  if FLAGS.use_gpu:
    logging.info('Use GPU')
    strategy = tf.distribute.MirroredStrategy()
  else:
    logging.info('Use TPU at %s',
                 FLAGS.tpu if FLAGS.tpu is not None else 'local')
    data_dir = FLAGS.data_dir
    resolver = tf.distribute.cluster_resolver.TPUClusterResolver(tpu=FLAGS.tpu)
    tf.config.experimental_connect_to_cluster(resolver)
    tf.tpu.experimental.initialize_tpu_system(resolver)
    strategy = tf.distribute.TPUStrategy(resolver)

  ds_info = tfds.builder(FLAGS.dataset).info
  batch_size = FLAGS.per_core_batch_size * FLAGS.num_cores
  train_dataset_size = (
      ds_info.splits['train'].num_examples * FLAGS.train_proportion)
  steps_per_epoch = int(train_dataset_size / batch_size)
  logging.info('Steps per epoch %s', steps_per_epoch)
  logging.info('Size of the dataset %s', ds_info.splits['train'].num_examples)
  logging.info('Train proportion %s', FLAGS.train_proportion)
  steps_per_eval = ds_info.splits['test'].num_examples // batch_size
  num_classes = ds_info.features['label'].num_classes

  train_dataset = ub.datasets.get(
      FLAGS.dataset,
      split=tfds.Split.TRAIN,
      download_data=True,
      validation_percent=1. - FLAGS.train_proportion,
      data_dir=data_dir).load(
          batch_size=batch_size)
  clean_test_dataset = ub.datasets.get(
      FLAGS.dataset,
      split=tfds.Split.TEST,
      data_dir=data_dir).load(batch_size=batch_size)
  train_dataset = strategy.experimental_distribute_dataset(train_dataset)
  test_datasets = {
      'clean': strategy.experimental_distribute_dataset(clean_test_dataset),
  }
  if FLAGS.corruptions_interval > 0:
    if FLAGS.dataset == 'cifar100':
      data_dir = FLAGS.cifar100_c_path
    corruption_types, _ = utils.load_corrupted_test_info(FLAGS.dataset)
    for corruption_type in corruption_types:
      for severity in range(1, 6):
        dataset = ub.datasets.get(
            f'{FLAGS.dataset}_corrupted',
            corruption_type=corruption_type,
            severity=severity,
            split=tfds.Split.TEST,
            data_dir=data_dir).load(batch_size=batch_size)
        test_datasets[f'{corruption_type}_{severity}'] = (
            strategy.experimental_distribute_dataset(dataset))

  summary_writer = tf.summary.create_file_writer(
      os.path.join(FLAGS.output_dir, 'summaries'))

  with strategy.scope():
    logging.info('Building ResNet model')
    model = ub.models.wide_resnet_heteroscedastic(
        input_shape=ds_info.features['image'].shape,
        depth=28,
        width_multiplier=10,
        num_classes=num_classes,
        l2=FLAGS.l2,
        hps=_extract_hyperparameter_dictionary(),
        version=2,
        temperature=FLAGS.temperature,
        num_factors=FLAGS.num_factors,
        num_mc_samples=FLAGS.num_mc_samples)
    logging.info('Model input shape: %s', model.input_shape)
    logging.info('Model output shape: %s', model.output_shape)
    logging.info('Model number of weights: %s', model.count_params())
    # Linearly scale learning rate and the decay epochs by vanilla settings.
    base_lr = FLAGS.base_learning_rate * batch_size / 128
    lr_decay_epochs = [(int(start_epoch_str) * FLAGS.train_epochs) // 200
                       for start_epoch_str in FLAGS.lr_decay_epochs]
    lr_schedule = ub.schedules.WarmUpPiecewiseConstantSchedule(
        steps_per_epoch,
        base_lr,
        decay_ratio=FLAGS.lr_decay_ratio,
        decay_epochs=lr_decay_epochs,
        warmup_epochs=FLAGS.lr_warmup_epochs)
    optimizer = tf.keras.optimizers.SGD(lr_schedule,
                                        momentum=0.9,
                                        nesterov=True)
    metrics = {
        'train/negative_log_likelihood':
            tf.keras.metrics.Mean(),
        'train/accuracy':
            tf.keras.metrics.SparseCategoricalAccuracy(),
        'train/loss':
            tf.keras.metrics.Mean(),
        'train/ece':
            um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
        'test/negative_log_likelihood':
            tf.keras.metrics.Mean(),
        'test/accuracy':
            tf.keras.metrics.SparseCategoricalAccuracy(),
        'test/ece':
            um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
    }
    if FLAGS.corruptions_interval > 0:
      corrupt_metrics = {}
      for intensity in range(1, 6):
        for corruption in corruption_types:
          dataset_name = '{0}_{1}'.format(corruption, intensity)
          corrupt_metrics['test/nll_{}'.format(dataset_name)] = (
              tf.keras.metrics.Mean())
          corrupt_metrics['test/accuracy_{}'.format(dataset_name)] = (
              tf.keras.metrics.SparseCategoricalAccuracy())
          corrupt_metrics['test/ece_{}'.format(dataset_name)] = (
              um.ExpectedCalibrationError(num_bins=FLAGS.num_bins))

    checkpoint = tf.train.Checkpoint(model=model, optimizer=optimizer)
    latest_checkpoint = tf.train.latest_checkpoint(FLAGS.output_dir)
    initial_epoch = 0
    if latest_checkpoint:
      # checkpoint.restore must be within a strategy.scope() so that optimizer
      # slot variables are mirrored.
      checkpoint.restore(latest_checkpoint)
      logging.info('Loaded checkpoint %s', latest_checkpoint)
      initial_epoch = optimizer.iterations.numpy() // steps_per_epoch

  @tf.function
  def train_step(iterator):
    """Training StepFn."""
    def step_fn(inputs):
      """Per-Replica StepFn."""
      images = inputs['features']
      labels = inputs['labels']
      with tf.GradientTape() as tape:
        logits = model(images, training=True)
        if FLAGS.label_smoothing == 0.:
          negative_log_likelihood = tf.reduce_mean(
              tf.keras.losses.sparse_categorical_crossentropy(labels,
                                                              logits,
                                                              from_logits=True))
        else:
          one_hot_labels = tf.one_hot(tf.cast(labels, tf.int32), num_classes)
          negative_log_likelihood = tf.reduce_mean(
              tf.keras.losses.categorical_crossentropy(
                  one_hot_labels,
                  logits,
                  from_logits=True,
                  label_smoothing=FLAGS.label_smoothing))
        l2_loss = sum(model.losses)
        loss = negative_log_likelihood + l2_loss
        # Scale the loss given the TPUStrategy will reduce sum all gradients.
        scaled_loss = loss / strategy.num_replicas_in_sync

      grads = tape.gradient(scaled_loss, model.trainable_variables)
      optimizer.apply_gradients(zip(grads, model.trainable_variables))

      probs = tf.nn.softmax(logits)
      metrics['train/ece'].add_batch(probs, label=labels)
      metrics['train/loss'].update_state(loss)
      metrics['train/negative_log_likelihood'].update_state(
          negative_log_likelihood)
      metrics['train/accuracy'].update_state(labels, logits)

    for _ in tf.range(tf.cast(steps_per_epoch, tf.int32)):
      strategy.run(step_fn, args=(next(iterator),))

  @tf.function
  def test_step(iterator, dataset_name):
    """Evaluation StepFn."""
    def step_fn(inputs):
      """Per-Replica StepFn."""
      images = inputs['features']
      labels = inputs['labels']
      logits = model(images, training=False)
      probs = tf.nn.softmax(logits)
      negative_log_likelihood = tf.reduce_mean(
          tf.keras.losses.sparse_categorical_crossentropy(labels, probs))

      if dataset_name == 'clean':
        metrics['test/negative_log_likelihood'].update_state(
            negative_log_likelihood)
        metrics['test/accuracy'].update_state(labels, probs)
        metrics['test/ece'].add_batch(probs, label=labels)
      else:
        corrupt_metrics['test/nll_{}'.format(dataset_name)].update_state(
            negative_log_likelihood)
        corrupt_metrics['test/accuracy_{}'.format(dataset_name)].update_state(
            labels, probs)
        corrupt_metrics['test/ece_{}'.format(dataset_name)].add_batch(
            probs, label=labels)

    for _ in tf.range(tf.cast(steps_per_eval, tf.int32)):
      strategy.run(step_fn, args=(next(iterator),))

  metrics.update({'test/ms_per_example': tf.keras.metrics.Mean()})
  metrics.update({'train/ms_per_example': tf.keras.metrics.Mean()})

  train_iterator = iter(train_dataset)
  start_time = time.time()
  tb_callback = None
  if FLAGS.collect_profile:
    tb_callback = tf.keras.callbacks.TensorBoard(
        profile_batch=(100, 102),
        log_dir=os.path.join(FLAGS.output_dir, 'logs'))
    tb_callback.set_model(model)
  for epoch in range(initial_epoch, FLAGS.train_epochs):
    logging.info('Starting to run epoch: %s', epoch)
    if tb_callback:
      tb_callback.on_epoch_begin(epoch)
    train_start_time = time.time()
    train_step(train_iterator)
    ms_per_example = (time.time() - train_start_time) * 1e6 / batch_size
    metrics['train/ms_per_example'].update_state(ms_per_example)

    current_step = (epoch + 1) * steps_per_epoch
    max_steps = steps_per_epoch * FLAGS.train_epochs
    time_elapsed = time.time() - start_time
    steps_per_sec = float(current_step) / time_elapsed
    eta_seconds = (max_steps - current_step) / steps_per_sec
    message = ('{:.1%} completion: epoch {:d}/{:d}. {:.1f} steps/s. '
               'ETA: {:.0f} min. Time elapsed: {:.0f} min'.format(
                   current_step / max_steps,
                   epoch + 1,
                   FLAGS.train_epochs,
                   steps_per_sec,
                   eta_seconds / 60,
                   time_elapsed / 60))
    logging.info(message)
    if tb_callback:
      tb_callback.on_epoch_end(epoch)
    datasets_to_evaluate = {'clean': test_datasets['clean']}
    if (FLAGS.corruptions_interval > 0 and
        (epoch + 1) % FLAGS.corruptions_interval == 0):
      datasets_to_evaluate = test_datasets
    for dataset_name, test_dataset in datasets_to_evaluate.items():
      test_iterator = iter(test_dataset)
      logging.info('Testing on dataset %s', dataset_name)
      logging.info('Starting to run eval at epoch: %s', epoch)
      test_start_time = time.time()
      test_step(test_iterator, dataset_name)
      ms_per_example = (time.time() - test_start_time) * 1e6 / batch_size
      metrics['test/ms_per_example'].update_state(ms_per_example)

      logging.info('Done with testing on %s', dataset_name)

    corrupt_results = {}
    if (FLAGS.corruptions_interval > 0 and
        (epoch + 1) % FLAGS.corruptions_interval == 0):
      corrupt_results = utils.aggregate_corrupt_metrics(corrupt_metrics,
                                                        corruption_types)

    logging.info('Train Loss: %.4f, Accuracy: %.2f%%',
                 metrics['train/loss'].result(),
                 metrics['train/accuracy'].result() * 100)
    logging.info('Test NLL: %.4f, Accuracy: %.2f%%',
                 metrics['test/negative_log_likelihood'].result(),
                 metrics['test/accuracy'].result() * 100)
    total_results = {name: metric.result() for name, metric in metrics.items()}
    total_results.update(corrupt_results)
    # Metrics from Robustness Metrics (like ECE) will return a dict with a
    # single key/value, instead of a scalar.
    total_results = {
        k: (list(v.values())[0] if isinstance(v, dict) else v)
        for k, v in total_results.items()
    }
    with summary_writer.as_default():
      for name, result in total_results.items():
        tf.summary.scalar(name, result, step=epoch + 1)

    for metric in metrics.values():
      metric.reset_states()

    if (FLAGS.checkpoint_interval > 0 and
        (epoch + 1) % FLAGS.checkpoint_interval == 0):
      checkpoint_name = checkpoint.save(
          os.path.join(FLAGS.output_dir, 'checkpoint'))
      logging.info('Saved checkpoint to %s', checkpoint_name)

  final_checkpoint_name = checkpoint.save(
      os.path.join(FLAGS.output_dir, 'checkpoint'))
  logging.info('Saved last checkpoint to %s', final_checkpoint_name)

if __name__ == '__main__':
  app.run(main)