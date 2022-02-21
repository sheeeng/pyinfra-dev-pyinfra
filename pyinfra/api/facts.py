'''
The pyinfra facts API. Facts enable pyinfra to collect remote server state which
is used to "diff" with the desired state, producing the final commands required
for a deploy.
'''

import re

from inspect import getcallargs, isclass
from socket import (
    error as socket_error,
    timeout as timeout_error,
)

import click
import gevent

from gevent.lock import BoundedSemaphore
from paramiko import SSHException

from pyinfra import logger
from pyinfra.api import StringCommand
from pyinfra.api.arguments import pop_global_arguments
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.util import (
    get_arg_value,
    get_kwargs_str,
    log_error_or_warning,
    log_host_command_error,
    make_hash,
    print_host_combined_output,
    underscore,
)
from pyinfra.connectors.util import split_combined_output
from pyinfra.progress import progress_spinner

from .arguments import get_executor_kwarg_keys


# Index of snake_case facts -> CamelCase classes
FACTS = {}
FACT_LOCK = BoundedSemaphore()

SUDO_REGEX = r'^sudo: unknown user:'
SU_REGEXES = (
    r'^su: user .+ does not exist',
    r'^su: unknown login',
)


def is_fact(name):
    return name in FACTS


def get_fact_class(name):
    return FACTS[name]


def get_fact_names():
    '''
    Returns a list of available facts in camel_case format.
    '''

    return list(FACTS.keys())


class FactMeta(type):
    '''
    Metaclass to dynamically build the facts index.
    '''

    def __init__(cls, name, bases, attrs):
        if attrs.get('abstract'):
            return

        fact_name = underscore(name)
        cls.name = fact_name

        # Get the an instance of the fact, attach to facts
        FACTS[fact_name] = cls


class FactBase(object, metaclass=FactMeta):
    abstract = True

    shell_executable = None

    requires_command = None

    @staticmethod
    def default():
        '''
        Set the default attribute to be a type (eg list/dict).
        '''

    @staticmethod
    def process(output):
        return '\n'.join(output)

    def process_pipeline(self, args, output):
        return {
            arg: self.process([output[i]])
            for i, arg in enumerate(args)
        }


class ShortFactBase(object, metaclass=FactMeta):
    fact = None


def get_short_facts(state, host, short_fact, **kwargs):
    facts = get_fact(state, host, short_fact.fact.name, **kwargs)

    return {
        host: short_fact.process_data(data)
        for host, data in facts.items()
    }


def _make_command(command_attribute, host_args):
    if callable(command_attribute):
        host_args.pop('self', None)
        return command_attribute(**host_args)
    return command_attribute


def _get_executor_kwargs(state, host, override_kwargs=None, override_kwarg_keys=None):
    if override_kwargs is None:
        override_kwargs = {}
        override_kwarg_keys = []

    # Apply any current op kwargs that *weren't* found in the overrides
    override_kwargs.update({
        key: value
        for key, value in (host.current_op_global_kwargs or {}).items()
        if key not in override_kwarg_keys
    })

    # Raise an exception if we have non-executor override kwargs (invalid in fact context)
    for key in override_kwarg_keys:
        if key not in get_executor_kwarg_keys():
            raise PyinfraError((
                'Global argument `_{0}` is not supported in facts.'
            ).format(key))

    return {
        key: value
        for key, value in override_kwargs.items()
        if key in get_executor_kwarg_keys()
    }


def get_facts(state, *args, **kwargs):
    greenlet_to_host = {
        state.pool.spawn(get_fact, state, host, *args, **kwargs): host
        for host in state.inventory.iter_active_hosts()
    }

    results = {}

    with progress_spinner(greenlet_to_host.values()) as progress:
        for greenlet in gevent.iwait(greenlet_to_host.keys()):
            host = greenlet_to_host[greenlet]
            results[host] = greenlet.get()
            progress(host)

    return results


def get_fact(
    state,
    host,
    name_or_cls,
    args=None,
    kwargs=None,
    ensure_hosts=None,
    apply_failed_hosts=True,
    fact_hash=None,
):
    with host.facts_lock:
        if fact_hash and fact_hash in host.facts:
            return host.facts[fact_hash]

        return _get_fact(
            state,
            host,
            name_or_cls,
            args,
            kwargs,
            ensure_hosts,
            apply_failed_hosts,
            fact_hash,
        )


def _get_fact(
    state,
    host,
    name_or_cls,
    args=None,
    kwargs=None,
    ensure_hosts=None,
    apply_failed_hosts=True,
    fact_hash=None,
):
    if isclass(name_or_cls) and issubclass(name_or_cls, (FactBase, ShortFactBase)):
        fact = name_or_cls()
        name = fact.name
    else:
        fact = get_fact_class(name_or_cls)()
        name = name_or_cls

    if isinstance(fact, ShortFactBase):
        return get_short_facts(state, host, fact, args=args, ensure_hosts=ensure_hosts)

    args = args or ()
    kwargs = kwargs or {}

    # Get the defaults *and* overrides by popping from kwargs, executor kwargs passed
    # into get_fact override everything else (applied below).
    override_kwargs, override_kwarg_keys = pop_global_arguments(state, host, kwargs)

    executor_kwargs = _get_executor_kwargs(
        state,
        host,
        override_kwargs=override_kwargs,
        override_kwarg_keys=override_kwarg_keys,
    )

    if args or kwargs:
        # Merges args & kwargs into a single kwargs dictionary
        kwargs = getcallargs(fact.command, *args, **kwargs)

    logger.debug('Getting fact: {0} ({1}) (ensure_hosts: {2})'.format(
        name, get_kwargs_str(kwargs), ensure_hosts,
    ))

    ignore_errors = (host.current_op_global_kwargs or {}).get(
        'ignore_errors',
        state.config.IGNORE_ERRORS,
    )

    # Facts can override the shell (winrm powershell vs cmd support)
    if fact.shell_executable:
        executor_kwargs['shell_executable'] = fact.shell_executable

    # Generate actual arguments by passing strings as jinja2 templates
    host_kwargs = {
        key: get_arg_value(state, host, arg)
        for key, arg in kwargs.items()
    }

    command = _make_command(fact.command, host_kwargs)
    requires_command = _make_command(fact.requires_command, host_kwargs)
    if requires_command:
        command = StringCommand(
            # Command doesn't exist, return 0 *or* run & return fact command
            '!', 'command', '-v', requires_command, '>/dev/null', '||', command,
        )

    status = False
    stdout = []

    try:
        status, combined_output_lines = host.run_shell_command(
            command,
            print_output=state.print_fact_output,
            print_input=state.print_fact_input,
            return_combined_output=True,
            **executor_kwargs,
        )
    except (timeout_error, socket_error, SSHException) as e:
        log_host_command_error(
            host, e,
            timeout=executor_kwargs['timeout'],
        )

    stdout, stderr = split_combined_output(combined_output_lines)

    data = fact.default()

    if status:
        if stdout:
            data = fact.process(stdout)
    elif stderr:
        # If we have error output and that error is sudo or su stating the user
        # does not exist, do not fail but instead return the default fact value.
        # This allows for users that don't currently but may be created during
        # other operations.
        first_line = stderr[0]
        if (executor_kwargs['sudo_user'] and re.match(SUDO_REGEX, first_line)):
            status = True
        if (executor_kwargs['su_user'] and any(
            re.match(regex, first_line) for regex in SU_REGEXES
        )):
            status = True

    if status:
        log_message = '{0}{1}'.format(
            host.print_prefix,
            'Loaded fact {0} ({1})'.format(
                click.style(name, bold=True),
                get_kwargs_str(kwargs),
            ),
        )
        if state.print_fact_info:
            logger.info(log_message)
        else:
            logger.debug(log_message)
    else:
        if not state.print_fact_output:
            print_host_combined_output(host, combined_output_lines)

        log_error_or_warning(host, ignore_errors, description=(
            'could not load fact: {0} {1}'
        ).format(name, get_kwargs_str(kwargs)))

    # Check we've not failed
    if not status and not ignore_errors and apply_failed_hosts:
        state.fail_hosts({host})

    if fact_hash:
        host.facts[fact_hash] = data
    return data


def get_host_fact(state, host, name, args=None, kwargs=None):
    fact_hash = make_hash((name, kwargs, _get_executor_kwargs(state, host)))
    return get_fact(state, host, name, args=args, kwargs=kwargs, fact_hash=fact_hash)


def create_host_fact(state, host, name, data, args=None, kwargs=None):
    fact_hash = make_hash((name, kwargs, _get_executor_kwargs(state, host)))
    host.facts[fact_hash] = data


def delete_host_fact(state, host, name, args=None, kwargs=None):
    fact_hash = make_hash((name, kwargs, _get_executor_kwargs(state, host)))
    host.facts.pop(fact_hash, None)
