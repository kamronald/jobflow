"""This module defines functions and classes for representing Job objects."""

from __future__ import annotations

import logging
import typing
import warnings
from dataclasses import dataclass, field

from monty.json import MSONable, jsanitize

from jobflow.core.reference import OutputReference, ReferenceFallback
from jobflow.utils.uuid import suuid

if typing.TYPE_CHECKING:
    from typing import Any, Callable, Dict, Hashable, List, Optional, Tuple, Type, Union

    from networkx import DiGraph
    from pydantic.main import BaseModel

    import jobflow

logger = logging.getLogger(__name__)

__all__ = ["job", "Job", "Response", "JobConfig", "store_output"]


@dataclass
class JobConfig(MSONable):
    """
    The configuration parameters for a job.

    Parameters
    ----------
    resolve_references
        Whether to resolve any references before the job function is executed.
        If ``False`` the unresolved reference objects will be passed into the function
        call.
    on_missing_references
        What to do if the references cannot be resolved. The default is to throw an
        error.
    manager_config
        The configuration settings to control the manager execution.
    expose_store
        Whether to expose the store in :obj:`.CURRENT_JOB`` when the job is running.

    Returns
    -------
    JobConfig
        A :obj:`JobConfig` object.
    """

    resolve_references: bool = True
    on_missing_references: ReferenceFallback = ReferenceFallback.ERROR
    manager_config: dict = field(default_factory=dict)
    expose_store: bool = False


def job(method: Optional[Callable] = None, **job_kwargs):
    """
    Wraps a function to produce a :obj:`Job`.

    :obj:`Job` objects are delayed function calls that can be used in an
    :obj:`Flow`. A job is a composed of the function name and source and any
    arguments for the function. This decorator makes it simple to create
    job objects directly from a function definition. See the examples for more details.

    Parameters
    ----------
    method
        A function to wrap. This should not be specified directly and is implied
        by the decorator.
    **job_kwargs
        Other keyword arguments that will get passed to the :obj:`Job` init method.

    Examples
    --------
    >>> @job
    ... def print_message():
    ...     print("I am a Job")
    >>> print_job = print_message()
    >>> type(print_job)
    <class 'jobflow.core.job.Job'>
    >>> print_job.function_source
    '__main__'
    >>> print_job.function_name
    'print_message'

    Jobs can have required and optional parameters.

    >>> @job
    ... def print_sum(a, b=0):
    ...     return print(a + b)
    >>> print_sum_job = print_sum(1, 2)
    >>> print_sum_job.function_args
    (1, )
    >>> print_sum_job.function_kwargs
    {"b": 2}

    If the function returns a value it can be referenced using the ``output``
    attribute of the job.

    >>> @job
    ... def add(a, b):
    ...     return a + b
    >>> add_task = add(1, 2)
    >>> add_task.output
    OutputReference('abeb6f48-9b34-4698-ab69-e4dc2127ebe9')

    .. Note::
        Because the task has not yet been run, the output value is :obj:`OutputReference`
        object. References are automatically converted to their computed values
        (resolved) when the task runs.

    If a dictionary of values is returned, the values can be indexed in the usual
    way.

    >>> @job
    ... def compute(a, b):
    ...     return {"sum": a + b, "product": a * b}
    >>> compute_task = compute(1, 2)
    >>> compute_task.output["sum"]
    OutputReference('abeb6f48-9b34-4698-ab69-e4dc2127ebe9', 'sum')

    .. Warning::
        If an output is indexed incorrectly, for example by trying to access a key that
        doesn't exist, this error will only be raised when the Job is executed.

    Jobs can return :obj:`.Response` objects that control the flow execution flow.
    For example, to replace the current jub with another job, ``replace`` can be used.

    >>> from jobflow import Response
    >>> @job
    ... def replace(a, b):
    ...     new_job = compute(a, b)
    ...     return Response(restart=new_job)

    See Also
    --------
    Job, .Flow, .Response
    """

    def decorator(func):
        from functools import wraps

        # unwrap staticmethod or classmethod decorators
        desc = next(
            (desc for desc in (staticmethod, classmethod) if isinstance(func, desc)),
            None,
        )

        if desc:
            func = func.__func__

        @wraps(func)
        def get_job(*args, **kwargs) -> Job:

            f = func
            if len(args) > 0:
                # see if the first argument has a function with the same name as
                # this function
                met = getattr(args[0], func.__name__, None)
                if met:
                    # if so, check to see if that function ha been wrapped and
                    # whether the unwrapped function is the same as this function
                    wrap = getattr(met, "__func__", None)
                    if getattr(wrap, "original", None) is func:
                        # Ah ha. The function is a bound method.
                        f = met
                        args = args[1:]

            return Job(
                function=f,
                function_args=args,
                function_kwargs=kwargs,
                **job_kwargs,
            )

        get_job.original = func

        if desc:
            # rewrap staticmethod or classmethod decorators
            get_job = desc(get_job)

        return get_job

    # See if we're being called as @job or @job().
    if method is None:
        # We're called with parens.
        return decorator

    # We're called as @job without parens.
    return decorator(method)


@dataclass
class Job(MSONable):
    """
    A :obj:`Job` is a delayed function call that can be used in an :obj:`.Flow`.

    In general, one should not create :obj:`Job` objects directly but instead use
    the :obj:`job` decorator on a function. Any calls to a decorated function will
    return an :obj:`Job` object.

    Parameters
    ----------
    function_source
        The source of the function. Can be ``"builtins"`` for builtin functions,
        a module name, or an :obj:`.MSONable` class instance. Additionally,
        static and class methods can be specified using a tuple of
        ``(module_name, class_name, bool)`` where the bool is set to ``True`` for class
        methods and ``False`` for static methods.
    function_name
        The function name.
    function_args
        The positional arguments to the function call.
    function_kwargs
        The keyword arguments to the function call.
    output_schema
        A :obj:`Schema` object that defines the schema of the output.
    uuid
        A unique identifier for the job. Generated automatically.
    index
        The index of the job (number of times the job has been replaced).
    name
        The name of the job. If not set it will be determined from ``function_source``
        and ``function_name``.
    metadata
        A dictionary of information that will get stored alongside the job output.
    config
        The config setting for the job.
    host
        The UUID of the host flow.

    Attributes
    ----------
    output
        The output of the job. This is a reference to the future job output and
        can be used as the input to other Jobs or Flows.

    Returns
    -------
    Job
        A job.

    Examples
    --------
    Builtin functions such as :obj:`print` can be specified using the ``builtins``
    module.

    >>> print_task = Job(function=("builtins", "print"), args=("I am a job", ))

    Other functions should specify the full module path.

    >>> Job(function=("os.path", "join"), args=("folder", "filename.txt"))

    To use custom functions in a job, the functions should be importable (i.e. not
    defined in another function). For example, if the following function is defined
    in the ``my_package`` module.

    >>> def add(a, b):
    ...     return a + b
    >>> add_job = Job(function=("my_package", "add"), args=(1, 2))

    More details are given in the :obj:`job` decorator docstring.

    See Also
    --------
    job, Response, .Flow
    """

    function: Callable
    function_args: Tuple[Any, ...] = field(default_factory=tuple)
    function_kwargs: Dict[str, Any] = field(default_factory=dict)
    output_schema: Optional[Type[BaseModel]] = None
    uuid: str = field(default_factory=suuid)
    index: int = 1
    name: Optional[str] = None
    data: Union[bool, str, Type[MSONable], List[Union[str, Type[MSONable]]]] = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    config: JobConfig = field(default_factory=JobConfig)
    host: Optional[str] = None
    output: OutputReference = field(init=False)

    def __post_init__(self):
        from jobflow.utils.find import contains_flow_or_job

        self.output = OutputReference(self.uuid, output_schema=self.output_schema)
        if self.name is None:
            if self.maker is not None:
                self.name = self.maker.name
            else:
                self.name = getattr(
                    self.function, "__qualname__", self.function.__name__
                )

        # check to see if job or flow is included in the job args
        # this is a possible situation but likely a mistake
        all_args = tuple(self.function_args) + tuple(self.function_kwargs.values())
        if contains_flow_or_job(all_args):
            warnings.warn(
                f"Job '{self.name}' contains an Flow or Job as an input. "
                f"Usually inputs should be the output of a Job or an Flow (e.g. "
                f"job.output). If this message is unexpected then double check the "
                f"inputs to your Job."
            )

    @property
    def input_references(self) -> Tuple[jobflow.OutputReference, ...]:
        """
        Find :obj:`.OutputReference` objects in the job inputs.

        Returns
        -------
        tuple(OutputReference, ...)
            The references in the inputs to the job.
        """
        from jobflow.core.reference import find_and_get_references

        references = set()
        for arg in tuple(self.function_args) + tuple(self.function_kwargs.values()):
            references.update(find_and_get_references(arg))

        return tuple(references)

    @property
    def input_uuids(self) -> Tuple[str, ...]:
        """

        Returns
        -------

        """
        return tuple([ref.uuid for ref in self.input_references])

    @property
    def input_references_grouped(self) -> Dict[str, Tuple[OutputReference, ...]]:
        from collections import defaultdict

        groups = defaultdict(set)
        for ref in self.input_references:
            groups[ref.uuid].add(ref)

        return {k: tuple(v) for k, v in groups.items()}

    @property
    def maker(self) -> Optional[jobflow.Maker]:
        from jobflow import Maker

        bound = getattr(self.function, "__self__", None)
        if isinstance(bound, Maker):
            return bound
        return None

    @property
    def graph(self) -> DiGraph:
        """
        Get a graph of the job indicating the inputs to the job.

        Returns
        -------
        DiGraph
            The graph showing the connectivity of the jobs.
        """
        from networkx import DiGraph

        edges = []
        for uuid, refs in self.input_references_grouped.items():
            properties = [
                ".".join(map(str, ref.attributes)) for ref in refs if ref.attributes
            ]
            properties = properties if len(properties) > 0 else "output"
            edges.append((uuid, self.uuid, {"properties": properties}))

        graph = DiGraph()
        graph.add_node(self.uuid, job=self, label=self.name)
        graph.add_edges_from(edges)
        return graph

    def set_uuid(self, uuid: str):
        """
        Set the UUID of the job.

        Parameters
        ----------
        uuid
            A UUID.
        """
        self.uuid = uuid
        self.output = self.output.set_uuid(uuid)

    def run(self, store: jobflow.JobStore) -> Response:
        """
        Run the job.

        If the job has inputs that are :obj:`.OutputReference` objects, then they will need
        to be resolved before the job can run. See the docstring for
        :obj:`.OutputReference.resolve()` for more details.

        Parameters
        ----------
        store
            A maggma store to use for resolving references and storing job outputs.

        Returns
        -------
        Response
            A the response of the job, containing the outputs, and other settings
            that determine the flow execution.

        Raises
        ------
        ValueError
            If the job function cannot be imported.

        See Also
        --------
        Response, .OutputReference
        """
        import types
        from datetime import datetime
        from jobflow import CURRENT_JOB

        index_str = f", {self.index}" if self.index != 1 else ""
        logger.info(f"Starting job - {self.name} ({self.uuid}{index_str})")
        CURRENT_JOB.job = self

        if self.config.expose_store:
            CURRENT_JOB.store = store

        if self.config.resolve_references:
            self.resolve_args(store=store)

        # if Job was created using the job decorator, then access the original function
        function = getattr(self.function, "original", self.function)

        # if function is bound method we need to do some magic to bind the unwrapped
        # function to the class/instance
        bound = getattr(self.function, "__self__", None)
        if bound is not None:
            function = types.MethodType(function, bound)

        response = function(*self.function_args, **self.function_kwargs)

        if not isinstance(response, Response):
            response = Response.from_job_returns(response, self.output_schema)

        if response.restart is not None:
            response.restart = prepare_restart(response.restart, self)

        save = "output" if self.data is True else self.data
        data = {
            "uuid": self.uuid,
            "index": self.index,
            "output": jsanitize(response.output, strict=True),
            "completed_at": datetime.now().isoformat(),
            "metadata": self.metadata,
        }
        store.update(data, key=["uuid", "index"], save=save)

        CURRENT_JOB.reset()
        logger.info(f"Finished job - {self.name} ({self.uuid}{index_str})")
        return response

    def resolve_args(
        self,
        store: jobflow.JobStore,
        on_missing: ReferenceFallback = ReferenceFallback.ERROR,
        inplace: bool = True,
    ) -> Job:
        """
        Resolve any :obj:`.OutputReference` objects in the input arguments.

        See the docstring for :obj:`.OutputReference.resolve()` for more details.

        Parameters
        ----------
        store
            A maggma store to use for resolving references.
        on_missing
            What to do if the reference cannot be resolved. See the docstring
            for :obj:`.OutputReference.resolve` for the available options.
        inplace
            Update the arguments of the current job or return a new job object.

        Returns
        -------
        Job
            A job with the references resolved.
        """
        from copy import deepcopy

        from jobflow.core.reference import find_and_resolve_references

        resolved_args = find_and_resolve_references(
            self.function_args,
            store=store,
            on_missing=on_missing,
        )
        resolved_kwargs = find_and_resolve_references(
            self.function_kwargs,
            store=store,
            on_missing=on_missing,
        )
        resolved_args = tuple(resolved_args)

        if inplace:
            self.function_args = resolved_args
            self.function_kwargs = resolved_kwargs
            return self

        new_job = deepcopy(self)
        new_job.function_args = resolved_args
        new_job.function_kwargs = resolved_kwargs
        return new_job

    def update_kwargs(
        self,
        update: Dict[str, Any],
        name_filter: Optional[str] = None,
        function_filter: Optional[Callable] = None,
        dict_mod: bool = False,
    ):
        """
        Update the kwargs of the jobs.

        Parameters
        ----------
        update
            The updates to apply.
        name_filter
            A filter for the job name.
        function_filter
            Only filter matching functions.
        dict_mod
            Use the dict mod language to apply updates. See :obj:`.DictMods` for more
            details.

        Examples
        --------
        Consider a flow containing a simple job with a ``number`` keyword argument.

        >>> from jobflow import job, Flow
        >>> @job
        ... def add(a, number=5):
        ...     return a + number
        >>> add_job = add(1)

        The ``number`` argument can be updated using.

        >>> add_job.update_kwargs({"number": 10})
        """
        from jobflow.utils.dict_mods import apply_mod

        if function_filter is not None and function_filter != self.function:
            return

        if name_filter is not None and name_filter not in self.name:
            return

        # if we get to here then we pass all the filters
        if dict_mod:
            apply_mod(update, self.function_kwargs)
        else:
            self.function_kwargs.update(update)

    def update_maker_kwargs(
        self,
        update: Dict[str, Any],
        name_filter: Optional[str] = None,
        class_filter: Optional[Type[jobflow.Maker]] = None,
        nested: bool = True,
        dict_mod: bool = False,
    ):
        """
        Update the keyword arguments of any :obj:`.Maker` objects in the job source.

        Parameters
        ----------
        update
            The updates to apply.
        name_filter
            A filter for the Maker name.
        class_filter
            A filter for the maker class. Note the class filter will match any
            subclasses.
        nested
            Whether to apply the updates to Maker objects that are themselves kwargs
            of a Maker object. See examples for more details.
        dict_mod
            Use the dict mod language to apply updates. See :obj:`.DictMods` for more
            details.

        Examples
        --------
        Consider the following job from a Maker:

        >>> from dataclasses import dataclass
        >>> from jobflow import job, Maker, Flow
        >>> @dataclass
        ... class AddMaker(Maker):
        ...     name: str = "add"
        ...     number: float = 10
        ...
        ...     @job
        ...     def make(self, a):
        ...         return a + self.number
        >>> maker = AddMaker()
        >>> add_job = maker.make(1)

        The ``number`` argument could be updated in the following ways.

        >>> add_job.update_maker_kwargs({"number": 10})

        By default, the updates are applied to nested Makers. These are Makers
        which are present in the kwargs of another Maker. Consider the following case
        for a Maker that produces a job that restarts.

        >>> from jobflow import Response
        >>> @dataclass
        ... class RestartMaker(Maker):
        ...     name: str = "restart"
        ...     add_maker: Maker = AddMaker()
        ...
        ...     @job
        ...     def make(self, a):
        ...         restart_job = self.add_maker.make(a)
        ...         return Response(restart=restart_job)
        >>> maker = RestartMaker()
        >>> my_job = maker.make(1)

        The following update will apply to the nested ``AddMaker`` in the kwargs of the
        ``RestartMaker``:

        >>> my_job.update_maker_kwargs({"number": 10}, function_filter=AddMaker)

        However, if ``nested=False``, then the update will not be applied to the nested
        Maker:

        >>> my_job.update_maker_kwargs(
        ...     {"number": 10}, function_filter=AddMaker, nested=False
        ... )
        """
        if self.maker is not None:
            maker = self.maker.update_kwargs(
                update,
                name_filter=name_filter,
                class_filter=class_filter,
                nested=nested,
                dict_mod=dict_mod,
            )
            self.function = getattr(maker, self.function.__name__)


@dataclass
class Response:
    """
    The :obj:`Response` contains the output, detours, and stop commands of a job.

    Parameters
    ----------
    output
        The job output.
    detour
        A flow or job to detour to.
    restart
        A flow or job to replace the current job.
    store
        Data to be stored by the flow manager.
    stop_children
        Stop any children of the current flow.
    stop_flows
        Stop executing all remaining jobs.
    """

    output: Optional[Any] = None
    restart: Optional[Union[jobflow.Flow, Job, List[Job]]] = None
    detour: Optional[Union[jobflow.Flow, Job, List[Job]]] = None
    addition: Optional[Union[jobflow.Flow, Job, List[Job]]] = None
    stored_data: Optional[Dict[Hashable, Any]] = None
    stop_children: bool = False
    stop_flows: bool = False

    @classmethod
    def from_job_returns(
        cls,
        job_returns: Optional[Any],
        output_schema: Optional[Type[BaseModel]] = None,
    ) -> Response:
        """
        Generate a :obj:`Response` from the outputs of a :obj:`Job`.

        Parameters
        ----------
        job_returns
            The outputs of a job. Should be a single or list of :obj:`Outputs`,
            :obj:`Store`, :obj:`Detour`, :obj:`Restart`, or :obj:`Stop` objects. Only
            one of each type of object is supported.

            .. Warning::
                :obj:`Detour` and :obj:`Outputs` objects should not be specified
                simultaneously. The outputs of the detour flow will be used instead.

        output_schema
            The outputs class associated with the job. Used to enforce a schema for the
            outputs. Currently, only a warning will be given if the job outputs do not
            match the expected outputs class.

        Returns
        -------
        Response
            The job response controlling the data to store and flow execution options.

        Raises
        ------
        ValueError
            If the job returns type ares not :obj:`Outputs`, :obj:`Store`, :obj:`Detour`,
            :obj:`Restart`, or :obj:`Stop` objects.
        ValueError
            If more than one of the same return type is given.

        See Also
        --------
        .Outputs, Store, Detour, Restart, Stop
        """
        if isinstance(job_returns, Response):
            if job_returns.restart is not None:
                # only apply output schema if there is no restart.
                job_returns.output = apply_schema(job_returns.output)

            return job_returns

        if isinstance(job_returns, (list, tuple)):
            # check that a Response object is not given as one of many outputs
            for r in job_returns:
                if isinstance(r, Response):
                    raise ValueError(
                        "Response cannot be returned in combination with other "
                        "outputs."
                    )

        return cls(output=apply_schema(job_returns, output_schema))


def apply_schema(output: Any, schema: Optional[Type[BaseModel]]):
    from pydantic import BaseModel

    # comparing schema instance is surprisingly fickle.
    if schema is None or (
        isinstance(output, BaseModel)
        and output.__class__.__name__ == schema.__name__
        and output.__module__ == schema.__module__
    ):
        return output

    if output is None:
        raise ValueError(f"Expected output of type {schema.__name__} but got no output")

    if not isinstance(output, dict):
        raise ValueError(
            f"Expected output to be {schema.__name__} or dict but got output type "
            f"of {type(output).__name__}."
        )

    return schema(**output)


@job(
    config=JobConfig(
        resolve_references=False, on_missing_references=ReferenceFallback.NONE
    )
)
def store_output(outputs: Any):
    return outputs


def prepare_restart(
    restart: Union[jobflow.Flow, Job, List[Job]],
    current_job: Job,
):
    from jobflow.core.flow import Flow

    if isinstance(restart, (list, tuple)):
        restart = Flow(jobs=restart)

    if isinstance(restart, Flow) and restart.output is not None:
        # add a job with same uuid as the current job to store the outputs of the
        # flow; this job will inherit the metadata and output schema of the current
        # job
        store_output_job = store_output(restart.output)
        store_output_job.config.manager_config = current_job.config.manager_config
        store_output_job.set_uuid(current_job.uuid)
        store_output_job.index = current_job.index + 1
        store_output_job.metadata = current_job.metadata
        store_output_job.output_schema = current_job.output_schema
        restart.jobs.append(store_output_job)

    else:
        # restart is a single Job
        restart.set_uuid(current_job.uuid)
        restart.index = current_job.index + 1

        metadata = restart.metadata
        metadata.update(current_job.metadata)
        restart.metadata = metadata

        if not restart.output_schema:
            restart.output_schema = current_job.output_schema

    return restart