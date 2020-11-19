import click
import inspect
import os
import logging
import gym
import time
import yaml
import traceback

import cibi
from cibi import bf
from cibi.utils import ensure_enough_test_runs, get_project_dir, calc_hash, update_keys
from cibi.codebase import make_prod_codebase
from cibi.extensions import make_gym
from cibi.teams import teams
from cibi.scrum_master import hire_team
from cibi.stream_discretizer import burn_in

logging.basicConfig(format='%(asctime)s %(message)s')
logger = logging.getLogger('cibi')

def make_seed_codebase(seed_file, env, observation_discretizer, action_sampler):
    seed_codebase = None

    if seed_file:
        try:
            with open(seed_file, 'r') as f:
                seed_codebase = make_prod_codebase(deduplication=True)
                for code in f.readlines():
                    seed_codebase.commit(code.strip(), metadata={
                        'author': 'god'
                    }, count=0)
                logger.info('Testing programs from seed codebase')
                ensure_enough_test_runs(seed_codebase, env, observation_discretizer, action_sampler)
                logger.info(seed_codebase.to_string())
                
        except OSError as e:
            logger.error(e)

    return seed_codebase

@click.command()
@click.argument('logdir', type=str)
def run_experiments(logdir):
    with open(os.path.join(logdir, 'experiment.yml'), 'r') as f:
        config = yaml.safe_load(f)
        config_hash = calc_hash(config)

    required_version = str(config['cibi-version'])
    assert cibi.__version__.startswith(required_version)

    render = config.get('render', False)
    discretization_config = config.get('discretization', {})

    scrum_keys = ['sprint-length', 'stretch-sprints',
                  'cycle-programs', 'syntax-error-reward', 'replay-temperature']
    scrum_config = {key.replace('-', '_'): config[key] for key in scrum_keys if key in config}

    max_failed_sprints = config.get('max-failed-sprints', 10)
    max_sprints = config.get('max-sprints', 1000000)
    max_sprints_without_improvement = config.get('max-sprints-without-improvement', 1000000)
    os.makedirs(logdir, exist_ok=True)

    scrum_config['program_file'] = os.path.join(logdir, 'programs.pickle')

    logger.setLevel(config.get('log_level', 'INFO'))
    logger.addHandler(logging.FileHandler(f'{logdir}/log.log'))
    logger.info(config['env'])
    start_time = time.monotonic()

    team = teams[config['team']]
    env = make_gym(config['env'])

    seed = config.get('seed')
    if seed:
        hardcoded_path = os.path.join(get_project_dir('codebases'), config['seed'])
        custom_path = os.path.join(logdir, config['seed'])
        
        if os.path.isfile(hardcoded_path):
            seed = hardcoded_path
        elif os.path.isfile(custom_path):
            seed = custom_path
        else:
            err = f"Seed codebase {config['seed']} not found"
            logger.error(err)
            raise FileNotFoundError(err)

    try:
        with open(os.path.join(logdir, 'summary.yml'), 'r') as f:
            summary = yaml.safe_load(f)
            if 'experiment' in summary and config_hash != summary['experiment']:
                summary = None
    except FileNotFoundError as e:
        summary = None
      
    if summary is None:
        summary = {
            'shortest-episode': float('inf'),
            'longest-episode': 0,
            'sprints-elapsed': 0,
            'seconds-elapsed': 0,
            'max-total-reward': float('-inf'),
            'experiment': config_hash
        }
    
    summary['cibi-version'] = cibi.__version__
    scrum_config['sprints_elapsed'] = summary['sprints-elapsed']
    start_time -= summary['seconds-elapsed']

    train_dir = os.path.join(logdir, 'train')
    events_dir = os.path.join(logdir, 'events')

    observation_discretizer = bf.ObservationDiscretizer(env.observation_space, 
                                                        history_length=discretization_config.get('history', 1024),
                                                        force_fluid=discretization_config.get('force-history', False))
    action_sampler = bf.ActionSampler(env.action_space)
    language = bf.make_bf_plus(config.get('allowed-commands', bf.DEFAULT_CMD_SET))

    random_agent = bf.Executable('@!', observation_discretizer, action_sampler, cycle=True, debug=False)
    burn_in(env, random_agent, observation_discretizer, action_sampler)
    seed_codebase = make_seed_codebase(seed, env, observation_discretizer, action_sampler)

    failed_sprints = 0
    sprints_of_last_improvement = 0
    with hire_team(team, env, observation_discretizer, action_sampler, language,
                train_dir, events_dir, scrum_config, seed_codebase) as agent:
        max_episode_length = config.get('max-episode-length', max_sprints * agent.sprint_length)

        while (agent.sprints_elapsed < max_sprints 
           and summary['sprints-elapsed'] - sprints_of_last_improvement < max_sprints_without_improvement):
            try:
                rollout = agent.attend_gym(env, max_reps=max_episode_length, render=render)

                episode_length = len(rollout)
                summary['shortest-episode'] = min(summary['shortest-episode'], episode_length)
                summary['longest-episode'] = max(summary['longest-episode'], episode_length)
                if summary['max-total-reward'] < rollout.total_reward:
                    summary['max-total-reward'] = float(rollout.total_reward)
                    
                summary['sprints-elapsed'] = agent.sprints_elapsed
                summary['seconds-elapsed'] = time.monotonic() - start_time

                with open(os.path.join(logdir, 'summary.yml'), 'w') as f:
                    yaml.dump(summary, f)

                failed_sprints = 0
            except Exception as e:
                logger.error(traceback.format_exc())
                failed_sprints += 1
                if failed_sprints > max_failed_sprints:
                    logger.error('Tolerance for failed sprints exceeded')
                    raise e

        logger.info(f'Summary: {summary}')
        
        top_candidates = agent.archive_branch.top_k('test_quality', 256)
        ensure_enough_test_runs(top_candidates, env, observation_discretizer, action_sampler)
        top_candidates.data_frame.to_pickle(os.path.join(logdir, 'top.pickle'))

if __name__ == '__main__':
    run_experiments()