from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

"""Extended BrainF**k interpreter.

Language info: https://en.wikipedia.org/wiki/Brainfuck
+ 0 to set memory to null value
+ ! to write to the beginning of the stack
"""

from cibi.agent import Agent, ActionError
from cibi.stream_discretizer import StreamDiscretizer, FluidStreamDiscretizer, DummyStreamDiscretizer

from collections import namedtuple
import gym.spaces as s
import numpy as np
import time
import itertools
import re

from typing import TYPE_CHECKING

ExecutionSnapshot = namedtuple(
    'ExecutionSnapshot',
    ['codeptr', 'codechar', 'memptr', 'memval', 'memory', 'action_stack', 'state'])

ObservationSnaphot = namedtuple(
  'ObservationSnaphot',
  ['raw_observaton', 'memory_update'])

ActionSnapshot = namedtuple(
  'ActionSnapshot',
  ['action_stack', 'raw_action', 'action_taken']
)

class Result(object):
  SUCCESS = 'success'
  STEP_LIMIT = 'step-limit'
  SYNTAX_ERROR = 'syntax-error'
  KILLED = 'killed'

class State(object):
  NOT_STARTED = 'not-started'
  EXECUTING = 'executing'
  AWAITING_INPUT = 'awaiting-input'
  FINISHED = 'finished'

DEFAULT_STEPS = 5
BF_EOS_INT = 0  # Also used as SOS (start of sequence).
BF_EOS_CHAR = TEXT_EOS_CHAR = '_'

def buildbracemap(code):
  """Build jump map.

  Args:
    code: List or string or BF chars.

  Returns:
    bracemap: dict mapping open and close brace positions in the code to their
        destination jumps. Specifically, positions of matching open/close braces
        if they exist.
    correct_syntax: True if all braces match. False if there are unmatched
        braces in the code. Even if there are unmatched braces, a bracemap will
        be built, and unmatched braces will map to themselves.
  """
  bracestack, bracemap = [], {}

  correct_syntax = True
  for position, command in enumerate(code):
    if command == '[':
      bracestack.append(position)
    if command == ']':
      if not bracestack:  # Unmatched closing brace.
        bracemap[position] = position  # Don't jump to any position.
        correct_syntax = False
        continue
      start = bracestack.pop()
      bracemap[start] = position
      bracemap[position] = start
  if bracestack:  # Unmatched opening braces.
    for pos in bracestack:
      bracemap[pos] = pos  # Don't jump to any position.
      correct_syntax = False
  return bracemap, correct_syntax

class ProgramFinishedError(ActionError):
  def __init__(self, code, program_result):
    msg = f'Trying to execute program {code} that has finished with {program_result}'
    super().__init__(msg, program_result)

class ObservationDiscretizer():
  def __init__(self, observation_space, history_length, thresholds=None, debug=False, force_fluid=False):
    thresholds = np.array(thresholds)

    if type(observation_space) == s.Box:
      if thresholds:
        if len(thresholds.shape) == 1:
          self.discretizers = [StreamDiscretizer(thresholds) 
                               for _ in range(observation_space.shape[0])]
        else:
          assert thresholds.shape[:-1] == observation_space.shape
          self.discretizers = [StreamDiscretizer(t) for t in thresholds.reshape(-1)]
      else:
        self.discretizers = []

        lower_bounds = observation_space.low.reshape(-1)
        upper_bounds = observation_space.high.reshape(-1)

        for lower_bound, upper_bound in zip(lower_bounds, upper_bounds):
          # Observation space object has bounded_below and bounded_above
          # attributes, but those are unreliable and in many environments
          # just always set to True
          bounded_below = (lower_bound > float('-3.4e+38'))
          bounded_above = (upper_bound < float('3.4e+38'))

          if (not force_fluid) and bounded_below and bounded_above:
            thresholds = np.linspace(lower_bound, upper_bound, num=DEFAULT_STEPS+1)[1:-1]
            discretizer = StreamDiscretizer(thresholds)
          else:
            discretizer = FluidStreamDiscretizer(bin_count=DEFAULT_STEPS, 
                                                 history_length=history_length) 

          self.discretizers.append(discretizer)            
    elif type(observation_space) in (s.Discrete, s.MultiDiscrete, s.MultiBinary):
      self.discretizers = []
    else:
      msg = f'{type(observation_space)} observation spaces not supported'
      raise NotImplementedError(msg)

    if debug:
      self.trace = []

    self.debug = debug

  def discretize(self, observation):
    if self.discretizers:
      observation = np.array(observation)
      discretized = [_discretize(feature) for _discretize, feature in zip(self.discretizers, observation.reshape(-1))]
      discretized = np.array(discretized).reshape(observation.shape)    
    else:
      discretized = observation
    
    if self.debug:
      self.trace.append(ObservationSnaphot(raw_observaton=observation,
                                           memory_update=discretized))
    
    return discretized

  def get_thresholds(self):
    return [d.thresholds for d in self.discretizers]

  def is_fluid(self):
    return any(type(d) == FluidStreamDiscretizer for d in self.discretizers)

  def is_saturated(self):
    return all(d.saturated for d in self.discretizers)

  def __call__(self, observation):
    return self.discretize(observation)

class ActionSampler:
  def __init__(self, action_space, discretization_steps=DEFAULT_STEPS, default_action=None, debug=False):
    space_type = type(action_space)
    self.sample_shape = action_space.shape
    self.discretization_steps = discretization_steps
    self.debug = debug
    self.just_a_number = False

    if debug:
      self.trace = []

    if space_type == s.Discrete:
      self.lower_bound = np.array([0])
      self.upper_bound = np.array([action_space.n - 1])
      self.bounded_below = np.array([True])
      self.bounded_above = np.array([True])
      self.default_action = 0
      self.sample_shape = (1,)
      self.just_a_number = True

      # Optional, but mapping 0-15 to 0 and 16-31 to 1 
      # in a binary case is an unnecessary complication
      # So let's not
      self.discretization_steps = action_space.n

    elif space_type == s.MultiDiscrete:
      self.upper_bound = action_space.nvec
      self.lower_bound = np.zeros_like(self.upper_bound)
      self.bounded_above = np.full(self.upper_bound.shape, True)
      self.bounded_below = np.full(self.lower_bound.shape, True)
      self.default_action = self.lower_bound

    elif space_type == s.MultiBinary:
      self.lower_bound = np.zeros(action_space.n)
      self.upper_bound = np.ones(action_space.n)
      self.bounded_above = np.full(self.upper_bound.shape, True)
      self.bounded_below = np.full(self.upper_bound.shape, True)
      self.default_action = self.lower_bound

    elif space_type == s.Box:
      self.lower_bound = action_space.low
      self.upper_bound = action_space.high
      self.bounded_above = action_space.bounded_above
      self.bounded_below = action_space.bounded_below
      self.default_action = np.zeros(self.sample_shape, dtype=np.float)

    else:
      raise NotImplementedError('Only Discrete, MultiDiscrete, MultiBinary and Box spaces are supported')

    # Override defaults
    if default_action is not None:
      self.default_action = default_action

  def undiscretize(self, discrete_actions):
    # See the paper for formula and explanation
    discrete_actions = discrete_actions.astype(np.int)

    both_bounds = self.bounded_above * self.bounded_below
    lower_bound_only = ~self.bounded_above * self.bounded_below
    upper_bound_only = self.bounded_above * ~self.bounded_below
    lower_bound = self.lower_bound[lower_bound_only]
    upper_bound = self.upper_bound[upper_bound_only]

    actions = np.zeros_like(discrete_actions)
    actions[both_bounds] = np.mod(discrete_actions[both_bounds], self.discretization_steps) * (self.upper_bound[both_bounds] - self.lower_bound[both_bounds] + 1)
    actions[~both_bounds] = discrete_actions[~both_bounds]
    actions = actions / self.discretization_steps
    flip = actions[lower_bound_only] < lower_bound
    actions[lower_bound_only][flip] = lower_bound - actions[lower_bound_only][flip]
    flip = actions[upper_bound_only] > upper_bound
    actions[upper_bound_only][flip] = upper_bound - actions[upper_bound_only][flip]

    return actions

  def sample(self, action_stack):
    sample_size = int(np.prod(self.sample_shape))

    # like pop(), but for many elements
    raw_action = None
    action = self.default_action

    if len(action_stack) >= sample_size:
      raw_action = action_stack[-sample_size:]
      del action_stack[-sample_size:]

      raw_action = np.array(raw_action).reshape(self.sample_shape)
      action = self.undiscretize(raw_action)
      if self.just_a_number:
        action = int(action)
    
    if self.debug:
      self.trace.append(ActionSnapshot(action_stack=action_stack,
                                       raw_action=raw_action,
                                       action_taken=action))

    return action

  def __call__(self, action_stack):
    return self.sample(action_stack)

DIGITS = '0123456789'
LETTERS = 'abcdefghijklmnopqrstuvwxyz'
DEFAULT_CMD_SET = '@><^+-[].,!~01234abcde'

def make_bf_plus(allowed_commands=DEFAULT_CMD_SET):
  BF_INT_TO_CHAR = BF_EOS_CHAR + allowed_commands
  BF_CHAR_TO_INT = dict([(c, i) for i, c in enumerate(BF_INT_TO_CHAR)])
  pointer_actions = LETTERS + '><'
  memory_actions = DIGITS + '\+\-'

  def bf_int_to_char(code_indices):
    code = ''.join(BF_INT_TO_CHAR[i] for i in code_indices)
    return code

  def bf_char_to_int(code):
    code_indices = [BF_CHAR_TO_INT[c] for c in code]
    return code_indices

  def prune_step(code):
    code = re.sub(r'\+\-|><|\-\+|<>', '', code)
    code = re.sub(fr'[{pointer_actions}]+(?=[{LETTERS}])', '', code)
    code = re.sub(fr'[{memory_actions}]+(?=[{DIGITS}])', '', code)
    return code

  def prune(code):
    old_code = None
    new_code = code

    while new_code != old_code:
      old_code, new_code = new_code, prune_step(new_code)

    return new_code

  return {
    'alphabet': BF_INT_TO_CHAR,
    'int_to_char': bf_int_to_char,
    'char_to_int': bf_char_to_int,
    'eos_int': BF_EOS_INT,
    'eos_char': BF_EOS_CHAR,
    'prune': prune
  }

class Executable(Agent):
  def __init__(self, code, observation_discretizer, action_sampler,
               language=make_bf_plus(),
               metrics={}, metadata={},
               init_memory=None, null_value=0,
               max_steps=2 ** 12, require_correct_syntax=True, debug=False,
               cycle = False):
    self.code = code
    self.metrics = metrics
    self.metadata = metadata
    self.alphabet = language['alphabet']

    code = list(code)
    self.bracemap, correct_syntax = buildbracemap(code)  # will modify code list
    if len(code) == 0:
      # Empty programs are a very easy way for a lazy-bum developer
      # to avoid negative reinforcement for syntax errors
      # Not so fast, lazy-bum developers
      correct_syntax = False

    self.is_valid = correct_syntax or not require_correct_syntax

    self.observation_discretizer = observation_discretizer
    self.action_sampler = action_sampler
    
    self.init_memory = init_memory
    self.max_steps = max_steps
    self.debug = debug
    self.null_value = null_value
    self.cycle = cycle

    self.init()

  def init(self):
    self.program_trace = [] if self.debug else None
    self.codeptr, self.cellptr = 0, 0
    self.steps = 0
    self.cells = list(self.init_memory) if self.init_memory else [0]
    self.action_stack = []

    if not self.is_valid:
      self.state = State.FINISHED
      self.result = Result.SYNTAX_ERROR
    else:
      self.state = State.NOT_STARTED
      self.result = None

  def record_snapshot(self, command):
    if self.debug:
      # Add step to program trace.
      self.program_trace.append(ExecutionSnapshot(
          codeptr=self.codeptr, codechar=command, memptr=self.cellptr,
          memval=self.read(), memory=list(self.cells),
          state=self.state, action_stack=self.action_stack))

  def done(self):
    if self.state != State.FINISHED:
      self.state = State.FINISHED
      self.result = Result.KILLED

  def ensure_enough_cells(self, cells_required=1):
    cell_shortage = self.cellptr + cells_required - len(self.cells)
    if cell_shortage > 0:
      self.cells.extend(self.null_value for i in range(cell_shortage))

  def read(self):
    self.ensure_enough_cells()
    return self.cells[self.cellptr]

  def write(self, value):
    try:
      value = [int(value)]
    except (TypeError, ValueError):
      value = [int(number) for number in value]

    self.ensure_enough_cells(len(value))
    writeptr = self.cellptr
    for number in value:
      self.cells[writeptr] = number
      writeptr += 1

  def step(self):
    if self.state == State.FINISHED:
      raise ProgramFinishedError(self.code, self.result)
    if self.state == State.AWAITING_INPUT:
      return

    self.state = State.EXECUTING

    if self.max_steps is not None and self.steps >= self.max_steps:
      self.result = Result.STEP_LIMIT
      self.state = State.FINISHED
      return

    if self.codeptr == len(self.code):
      if self.cycle:
        self.codeptr = -1
        self.state = State.AWAITING_INPUT
        self.record_snapshot(',')
        return
      else:
        self.state = State.FINISHED
        self.result = Result.SUCCESS
        return

    command = self.code[self.codeptr]
    self.record_snapshot(command)

    if command in self.alphabet:
      if command == '>':
        self.cellptr += 1

      if command == '<':
        self.cellptr = 0 if self.cellptr <= 0 else self.cellptr - 1

      if command == '^':
        # I don't trust languages without GOTO
        goto = int(self.read())
        if goto >= 0:
          self.cellptr = goto

      if command == '+':
        value = self.read()
        value += 1
        self.write(value)

      if command == '-':
        value = self.read()
        value -= 1
        self.write(value)

      if command == '~':
        value = self.read()
        value = -value
        self.write(value) 

      if command == '@':
        self.write(np.random.randint(DEFAULT_STEPS))

      if command in DIGITS:
        self.write(DIGITS.index(command))

      if command in LETTERS:
        self.cellptr = LETTERS.index(command)

      if command == '[' and self.read() <= 0: self.codeptr = self.bracemap[self.codeptr]
      if command == ']' and self.read() > 0: self.codeptr = self.bracemap[self.codeptr]

      if command == '.': self.action_stack.insert(0, self.read())
      if command == '!': self.action_stack.append(self.read())

      if command == ',':
        self.state = State.AWAITING_INPUT
        self.record_snapshot(command)
        return

    self.codeptr += 1
    self.steps += 1

  def execute(self):
    self.step()

    while self.state == State.EXECUTING:
      self.step()

  def input(self, inp):
    if self.state == State.NOT_STARTED:
      # Execution usually starts with an input
      # We don't want to lose this input
      self.state = State.AWAITING_INPUT

    while self.state != State.AWAITING_INPUT:
      self.execute()

    self.write(self.observation_discretizer(inp))

    self.codeptr += 1
    self.steps = 0

    self.state = State.EXECUTING
    self.record_snapshot(',')

  def act(self):
    action = self.action_sampler.sample(self.action_stack)
    return action