import tensorflow as tf
import logging
import pickle
import os
import time

logger = logging.getLogger(__file__)

def make_initialized_variable(value, name, shape=None, dtype=tf.float32):
  """Create a tf.Variable with a constant initializer.

  Args:
    value: Constant value to initialize the variable with. This is the value
        that the variable starts with.
    name: Name of the variable in the TF graph.
    shape: Shape of the variable. If None, variable will be a scalar.
    dtype: Data type of the variable. Should be a TF dtype. Defaults to
        tf.float32.

  Returns:
    tf.Variable instance.
  """
  if shape is None:
    shape = []
  return tf.get_variable(
      name=name, shape=shape, initializer=tf.constant_initializer(value),
      dtype=dtype, trainable=False)

class Developer(object):
  """Writes code using 2 language models

  A global model on the parameter server, and a local
  model (for this worker). Gradient updates are sent to the global model, and
  the updated weights are synced to the local copy.
  """

  def __init__(self, config,
               make_language_model,
               task_id=0, ps_tasks=0, num_workers=1, is_chief=True,
               summary_writer=None,
               dtype=tf.float32,
               cycle_program=False,
               summary_interval=1,
               run_number=0,
               logging_dir='/tmp', model_v=0):
    self.task_id = task_id
    self.ps_tasks = ps_tasks
    self.is_chief = is_chief

    if ps_tasks == 0:
      assert task_id == 0, 'No parameter servers specified. Expecting 1 task.'
      assert num_workers == 1, (
          'No parameter servers specified. Expecting 1 task.')
      worker_device = '/job:localhost/replica:%d/task:0/cpu:0' % task_id
      # worker_device = '/cpu:0'
      # ps_device = '/cpu:0'
    else:
      assert num_workers > 0, 'There must be at least 1 training worker.'
      worker_device = '/job:worker/replica:%d/task:0/cpu:0' % task_id
      # ps_device = '/job:ps/replica:0/task:0/cpu:0'
    logger.info('worker_device: %s', worker_device)

    logging_file = os.path.join(
        logging_dir, 'solutions_%d.txt' % task_id)
    experience_replay_file = os.path.join(
        logging_dir, 'replay_buffer_%d.pickle' % task_id)
    self.topk_file = os.path.join(
        logging_dir, 'topk_buffer_%d.pickle' % task_id)

    tf.get_variable_scope().set_use_resource(True)

    # global model
    with tf.device(tf.train.replica_device_setter(ps_tasks,
                                                  ps_device='/job:ps/replica:0',
                                                  worker_device=worker_device)):
      with tf.variable_scope('global'):
        global_model = make_language_model(config, dtype=dtype, is_local=False, cycle_program=cycle_program)
        global_params_dict = {p.name: p
                              for p in global_model.sync_variables}
        self.global_model = global_model
        self.global_step = make_initialized_variable(
            0, 'global_step', dtype=tf.int64)

        self.global_best_reward = make_initialized_variable(
            -10.0, 'global_best_reward', dtype=tf.float64)
        self.is_best_model = make_initialized_variable(
            False, 'is_best_model', dtype=tf.bool)
        self.reset_is_best_model = self.is_best_model.assign(False)
        self.global_best_reward_placeholder = tf.placeholder(
            tf.float64, [], name='global_best_reward_placeholder')
        self.assign_global_best_reward_op = tf.group(
            self.global_best_reward.assign(
                self.global_best_reward_placeholder),
            self.is_best_model.assign(True))
        def assign_global_best_reward_fn(session, reward):
          reward = round(reward, 10)
          best_reward = round(session.run(self.global_best_reward), 10)
          is_best = reward > best_reward
          if is_best:
            session.run(self.assign_global_best_reward_op,
                        {self.global_best_reward_placeholder: reward})
          return is_best
        self.assign_global_best_reward_fn = assign_global_best_reward_fn

        self.run_number = make_initialized_variable(
            run_number, 'run_number', dtype=tf.int32)

        # Count all programs sampled from policy. This does not include
        # programs sampled from replay buffer.
        # This equals NPE (number of programs executed). Only programs sampled
        # from the policy need to be executed.
        self.program_count = make_initialized_variable(
            0, 'program_count', dtype=tf.int64)

    # local model
    with tf.device(worker_device):
      with tf.variable_scope('local'):
        self.model = model = make_language_model(
            config,
            logging_file=logging_file,
            cycle_program=cycle_program,
            experience_replay_file=experience_replay_file,
            dtype=dtype,
            global_best_reward_fn=self.assign_global_best_reward_fn,
            program_count=self.program_count,
            verbose_level=model_v)
        local_params = model.trainable_variables
        local_params_dict = {p.name: p for p in local_params}

    # Pull global params to local model.
    def _global_to_local_scope(name):
      assert name.startswith('global/')
      return 'local' + name[6:]
    sync_dict = {
        local_params_dict[_global_to_local_scope(p_name)]: p
        for p_name, p in global_params_dict.items()}
    self.sync_op = tf.group(*[v_local.assign(v_global)
                              for v_local, v_global
                              in sync_dict.items()])

    # Pair local gradients with global params.
    grad_var_dict = {
        gradient: sync_dict[local_var]
        for local_var, gradient in model.gradients_dict.items()}

    # local model
    model.make_summary_ops()  # Don't put summaries under 'local' scope.
    with tf.variable_scope('local'):
      self.train_op = model.optimizer.apply_gradients(
          grad_var_dict.items(), global_step=self.global_step)
      self.local_init_op = tf.variables_initializer(
          tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES,
                            tf.get_variable_scope().name))

    self.local_step = 0
    self.last_summary_time = time.time()
    self.summary_interval = summary_interval
    self.summary_writer = summary_writer
    self.cached_global_step = -1
    self.cached_global_npe = -1

    logger.info('summary_interval: %d', self.summary_interval)

    # Load top-k buffer.
    if self.model.top_episodes is not None and tf.gfile.Exists(self.topk_file):
      try:
        with tf.gfile.FastGFile(self.topk_file, 'r') as f:
          self.model.top_episodes = pickle.loads(f.read())
        logger.info(
            'Loaded top-k buffer from disk with %d items. Location: "%s"',
            len(self.model.top_episodes), self.topk_file)
      except (pickle.UnpicklingError, EOFError) as e:
        logger.warn(
            'Failed to load existing top-k buffer from disk. Removing bad file.'
            '\nLocation: "%s"\nException: %s', self.topk_file, str(e))
        tf.gfile.Remove(self.topk_file)

  def initialize(self, session):
    """Run initialization ops."""
    session.run(self.local_init_op)
    session.run(self.sync_op)
    self.cached_global_step, self.cached_global_npe = session.run(
        [self.global_step, self.program_count])

  def write_programs(self, session):
    session.run(self.sync_op)  # Copy weights from global to local.

    with session.as_default():
      return self.model.write_programs(session)

  def reflect(self, session, programs):
    """Run an update step.

    1) Asynchronously copy global weights to local model.
    2) Call into local model's update_step method, which does the following:
        a) Sample batch of programs from policy.
        b) Compute rewards.
        c) Compute gradients and update the global model asynchronously.
    3) Write tensorboard summaries to disk.

    Args:
      session: tf.Session instance.
    """
    session.run(self.sync_op)  # Copy weights from global to local.

    with session.as_default():
      result = self.model.reflect(session, programs, self.train_op,
          self.global_step)
      global_step = result.global_step
      global_npe = result.global_npe
      summaries = result.summaries_list
    self.cached_global_step = global_step
    self.cached_global_npe = global_npe
    self.local_step += 1

    if self.summary_writer and self.local_step % self.summary_interval == 0:
      if not isinstance(summaries, (tuple, list)):
        summaries = [summaries]
      summaries.append(self._local_step_summary())
      if self.is_chief:
        (global_best_reward,
         found_solution_flag,
         program_count) = session.run(
             [self.global_best_reward,
              self.found_solution_flag,
              self.program_count])
        summaries.append(
            tf.Summary(
                value=[tf.Summary.Value(
                    tag='model/best_reward',
                    simple_value=global_best_reward)]))
        summaries.append(
            tf.Summary(
                value=[tf.Summary.Value(
                    tag='model/solution_found',
                    simple_value=int(found_solution_flag))]))
        summaries.append(
            tf.Summary(
                value=[tf.Summary.Value(
                    tag='model/program_count',
                    simple_value=program_count)]))
      for s in summaries:
        self.summary_writer.add_summary(s, global_step)
      self.last_summary_time = time.time()

  def _local_step_summary(self):
    """Compute number of local steps per time increment."""
    dt = time.time() - self.last_summary_time
    steps_per_time = self.summary_interval / float(dt)
    return tf.Summary(value=[
        tf.Summary.Value(
            tag='local_step/per_sec',
            simple_value=steps_per_time),
        tf.Summary.Value(
            tag='local_step/step',
            simple_value=self.local_step)])

  def maybe_save_best_model(self, session, saver, checkpoint_file):
    """Check if this model got the highest reward and save to disk if so."""
    if self.is_chief and session.run(self.is_best_model):
      logger.info('Saving best model to "%s"', checkpoint_file)
      saver.save(session, checkpoint_file)
      session.run(self.reset_is_best_model)

  def save_replay_buffer(self):
    """Save replay buffer to disk.

    Call this periodically so that training can recover if jobs go down.
    """
    if self.model.experience_replay is not None:
      logger.info('Saving experience replay buffer to "%s".',
                   self.model.experience_replay.save_file)
      self.model.experience_replay.incremental_save(True)

  def delete_replay_buffer(self):
    """Delete replay buffer from disk.

    Call this at the end of training to clean up. Replay buffer can get very
    large.
    """
    if self.model.experience_replay is not None:
      logger.info('Deleting experience replay buffer at "%s".',
                   self.model.experience_replay.save_file)
      tf.gfile.Remove(self.model.experience_replay.save_file)

  def save_topk_buffer(self):
    """Save top-k buffer to disk.

    Call this periodically so that training can recover if jobs go down.
    """
    if self.model.top_episodes is not None:
      logger.info('Saving top-k buffer to "%s".', self.topk_file)
      # Overwrite previous data each time.
      with tf.gfile.FastGFile(self.topk_file, 'w') as f:
        f.write(pickle.dumps(self.model.top_episodes))

class FullStackDeveloper():
  """
  A developer that knows their Tensorflow session so that 
  you don't have to hold their hand while they're coding
  """

  def __init__(self, developer, session):
    self.developer = developer
    self.session = session

  def write_programs(self):
    return self.developer.write_programs(self.session)

  def reflect(self, programs):
    return self.reflect(self.session, programs)