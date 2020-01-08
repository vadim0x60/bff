import logging
logger = logging.getLogger(f'cibi.{__file__}')
import itertools

from cibi.rollout import Rollout

class ActionError(Exception):
    """This agent cannot act at the moment"""
    def __init__(self, message='This agent cannot act at the moment', details=None):
        super().__init__(message)
        self.details = details

class Agent():
    def input(self, inp):
        pass

    def act(self):
        raise NotImplementedError

    def value(self):
        """Ask the agent how it's doing. Quantitative estimates only"""
        return 0

    def reward(self, reward):
        pass

    def init(self):
        raise NotImplementedError

    def done(self):
        raise NotImplementedError

    def attend_gym(self, env, max_reps=1000, render=True):
        rollout = Rollout()
        self.init()

        try:
            observation = env.reset()
            rng = range(max_reps) if max_reps else itertools.count()

            for _ in rng:
                self.input(observation)

                if render:
                    try:
                        env.render()  
                    except NotImplementedError:
                        render = False
                    
                action = self.act()
                prev_observation = observation
                observation, reward, done, info = env.step(action)
                rollout.add(prev_observation, action, reward, self.value(), done)
                self.reward(reward)

                if done:
                    break
                
        except ActionError as e:
            logger.warn(f'Gym training finished prematurely: {e}')

        self.done()
        env.close()

        return rollout