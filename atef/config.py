from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import (Any, Dict, Generator, List, Literal, Optional, Sequence,
                    Tuple, Union, cast)

import apischema
import happi
import ophyd
import yaml
from ophyd.signal import ConnectionTimeoutError

from . import serialization, tools, util
from .cache import DataCache
from .check import Comparison, Result
from .enums import GroupResultMode, Severity
from .exceptions import PreparedComparisonException
from .type_hints import AnyPath
from .yaml_support import init_yaml_support

logger = logging.getLogger(__name__)


@dataclass
@serialization.as_tagged_union
class Configuration:
    """
    Configuration base class for shared settings between all configurations.

    Subclasses of Comparison will be serialized as a tagged union.  This means
    that the subclass name will be used as an identifier for the generated
    serialized dictionary (and JSON object).
    """

    #: Name tied to this configuration.
    name: Optional[str] = None
    #: Description tied to this configuration.
    description: Optional[str] = None
    #: Tags tied to this configuration.
    tags: Optional[List[str]] = None


@dataclass
class ConfigurationGroup(Configuration):
    """
    Configuration group.
    """
    #: Configurations underneath this group.
    configs: List[Configuration] = field(default_factory=list)
    #: Values that can be reused in comparisons underneath this group.
    values: Dict[str, Any] = field(default_factory=dict)
    #: Result mode.
    mode: GroupResultMode = GroupResultMode.all_

    def walk_configs(self) -> Generator[AnyConfiguration, None, None]:
        for config in self.configs:
            # `config` is stored as Configuration due to the tagged union;
            # however we never yield Configuration instances, just subclasses
            # thereof:
            config = cast(AnyConfiguration, config)
            yield config
            if isinstance(config, ConfigurationGroup):
                yield from config.walk_configs()


@dataclass
class DeviceConfiguration(Configuration):
    """
    A configuration that is built to check one or more devices.

    Identifiers are by default assumed to be attribute (component) names of the
    devices.  Identifiers may refer to components on the device
    (``"component"`` would mean to access each device's ``.component``) or may
    refer to any level of sub-device components (``"sub_device.component"``
    would mean to access each device's ``.sub_device`` and that sub-device's
    ``.a`` component).
    """
    #: The device names.
    devices: List[str] = field(default_factory=list)
    #: Identifier name to comparison list.
    by_attr: Dict[str, List[Comparison]] = field(default_factory=dict)
    #: Comparisons to be run on *all* identifiers in the `by_attr` dictionary.
    shared: List[Comparison] = field(default_factory=list)


@dataclass
class PVConfiguration(Configuration):
    """
    A configuration that is built to check live EPICS PVs.

    Identifiers are by default assumed to be PV names.
    """
    #: PV name to comparison list.
    by_pv: Dict[str, List[Comparison]] = field(default_factory=dict)
    #: Comparisons to be run on *all* identifiers in the `by_pv` dictionary.
    shared: List[Comparison] = field(default_factory=list)


@dataclass
class ToolConfiguration(Configuration):
    """
    A configuration unrelated to PVs or Devices which verifies status via some
    tool.

    Comparisons can optionally be run on the tool's results.
    """
    #: The tool and its settings.  Subclasses such as "Ping" are expected
    #: here.
    tool: tools.Tool = field(default_factory=tools.Ping)
    #: Result attribute name to comparison list.
    by_attr: Dict[str, List[Comparison]] = field(default_factory=dict)
    #: Comparisons to be run on *all* identifiers in the `by_attr` dictionary.
    shared: List[Comparison] = field(default_factory=list)


AnyConfiguration = Union[
    PVConfiguration,
    DeviceConfiguration,
    ToolConfiguration,
    ConfigurationGroup,
]


@dataclass
class ConfigurationFile:
    """
    A configuration file comprised of a number of devices/PV configurations.
    """
    #: atef configuration file version information.
    version: Literal[0] = field(default=0, metadata=apischema.metadata.required)
    #: Top-level configuration group.
    root: ConfigurationGroup = field(default_factory=ConfigurationGroup)

    def walk_configs(self) -> Generator[AnyConfiguration, None, None]:
        """
        Walk configurations defined in this file.  This includes the "root"
        node.

        Yields
        ------
        AnyConfiguration
        """
        yield self.root
        yield from self.root.walk_configs()

    def get_by_device(self, name: str) -> Generator[DeviceConfiguration, None, None]:
        """Get all configurations that match the device name."""
        for config in self.walk_configs():
            if isinstance(config, DeviceConfiguration):
                if name in config.devices:
                    yield config

    def get_by_pv(
        self, pvname: str
    ) -> Generator[PVConfiguration, None, None]:
        """Get all configurations + IdentifierAndComparison that match the PV name."""
        for config in self.walk_configs():
            if isinstance(config, PVConfiguration):
                if pvname in config.by_pv:
                    yield config

    def get_by_tag(self, *tags: str) -> Generator[Configuration, None, None]:
        """Get all configurations that match the tag name."""
        if not tags:
            return

        tag_set = set(tags)
        for config in self.walk_configs():
            if tag_set.intersection(set(config.tags or [])):
                yield config

    @classmethod
    def from_json(cls, filename: AnyPath) -> ConfigurationFile:
        """Load a configuration file from JSON."""
        with open(filename) as fp:
            serialized_config = json.load(fp)
        return apischema.deserialize(cls, serialized_config)

    @classmethod
    def from_yaml(cls, filename: AnyPath) -> ConfigurationFile:
        """Load a configuration file from yaml."""
        with open(filename) as fp:
            serialized_config = yaml.safe_load(fp)
        return apischema.deserialize(cls, serialized_config)

    def to_json(self):
        """Dump this configuration file to a JSON-compatible dictionary."""
        return apischema.serialize(ConfigurationFile, self, exclude_defaults=True)

    def to_yaml(self):
        """Dump this configuration file to yaml."""
        init_yaml_support()
        return yaml.dump(self.to_json())


@dataclass
class PreparedFile:
    #: The data cache to use for the preparation step.
    cache: DataCache = field(repr=False)
    #: The corresponding configuration file information.
    file: ConfigurationFile
    #: The happi client instance.
    client: happi.Client
    #: The comparisons defined in the top-level file.
    root: PreparedGroup

    @classmethod
    def from_config(
        cls,
        file: ConfigurationFile,
        *,
        client: Optional[happi.Client] = None,
        cache: Optional[DataCache] = None,
    ) -> PreparedFile:
        if client is None:
            client = happi.Client.from_config()

        if cache is None:
            cache = DataCache()

        prepared_file = PreparedFile(
            file=file,
            cache=cache,
            client=client,
            root=None,
        )
        prepared_root = PreparedGroup.from_group(
            file.root,
            client=client,
            cache=cache,
            parent=prepared_file,
        )
        prepared_root.parent = prepared_root
        prepared_file.root = prepared_root
        return prepared_file

    async def fill_cache(self, parallel: bool = True) -> Optional[List[asyncio.Task]]:
        """
        Fill the DataCache.
        """
        if not parallel:
            for prepared in self.walk_comparisons():
                if isinstance(prepared, PreparedComparison):
                    await prepared.get_data_async()
            return None

        tasks = []
        for prepared in self.walk_comparisons():
            if isinstance(prepared, PreparedComparison):
                task = asyncio.create_task(prepared.get_data_async())
                tasks.append(task)

        return tasks

    def walk_comparisons(
        self,
    ) -> Generator[Union[PreparedComparison, FailedConfiguration], None, None]:
        """
        Walk through the prepared comparisons.
        """
        yield from self.root.walk_comparisons()

    def walk_groups(
        self,
    ) -> Generator[Union[PreparedGroup], None, None]:
        """Walk through the prepared groups."""
        yield self.root
        yield from self.root.walk_groups()

    async def compare(self) -> Result:
        """Run all comparisons and return a combined result."""
        return await self.root.compare()


@dataclass
class FailedConfiguration:
    #: The data cache to use for the preparation step.
    parent: Optional[PreparedGroup]
    #: Configuration instance.
    config: AnyConfiguration
    #: Reason
    reason: Result
    #: Reason
    exception: Optional[Exception] = None


def _summarize_result_severity(
    mode: GroupResultMode,
    results: List[Union[Result, Exception, None]]
) -> Severity:
    """
    Summarize all results based on the configured mode.

    Parameters
    ----------
    mode : GroupResultMode
        The mode to apply to the results.
    results : list of (Result, Exception, or None)
        The list of results.

    Returns
    -------
    Severity
        The calculated severity.
    """
    if any(result is None or isinstance(result, Exception) for result in results):
        return Severity.error

    severities = [
        result.severity for result in results if isinstance(result, Result)
    ]

    if mode == GroupResultMode.all_:
        return util.get_maximum_severity(severities)

    if mode == GroupResultMode.any_:
        return util.get_minimum_severity(severities)

    return Severity.internal_error


@dataclass
class PreparedConfiguration:
    #: The data cache to use for the preparation step.
    cache: DataCache = field(repr=False)
    #: The data cache to use for the preparation step.
    parent: Optional[PreparedGroup] = None
    #: The comparisons that failed to be prepared.
    prepare_failures: List[PreparedComparisonException] = field(default_factory=list)
    #: The result of all comparisons.
    result: Result = field(default_factory=Result)

    @classmethod
    def from_config(
        cls,
        config: AnyConfiguration,
        parent: Optional[PreparedGroup] = None,
        *,
        client: Optional[happi.Client] = None,
        cache: Optional[DataCache] = None,
    ) -> Union[
        PreparedPVConfiguration,
        PreparedDeviceConfiguration,
        PreparedToolConfiguration,
        PreparedGroup,
        FailedConfiguration,
    ]:
        """
        Create one or more PreparedConfiguration instances from a
        given Configuration instance.

        If available, provide an instantiated happi Client and a data
        cache.  If unspecified, a configuration-derived happi Client will
        be instantiated and a global data cache will be utilized.

        It is recommended - but not required - to manage a data cache on a
        per-configuration basis.  Managing the global cache is up to the user.

        Parameters
        ----------
        config : {PV,Device,Tool}Configuration or ConfigurationGroup
            The configuration.
        client : happi.Client, optional
            A happi Client instance.
        cache : DataCache, optional
            The data cache to use for this and other similar comparisons.
        """
        if cache is None:
            cache = DataCache()

        if isinstance(config, PVConfiguration):
            return PreparedPVConfiguration.from_config(
                config=config,
                cache=cache,
                parent=parent,
            )
        if isinstance(config, ToolConfiguration):
            return PreparedToolConfiguration.from_config(
                cache=cache,
                config=config,
                parent=parent,
            )
        if isinstance(config, DeviceConfiguration):
            return PreparedDeviceConfiguration.from_config(
                cache=cache,
                config=config,
                client=client,
                parent=parent,
            )
        if isinstance(config, ConfigurationGroup):
            return PreparedGroup.from_group(
                config,
                cache=cache,
                client=client,
                parent=parent,
            )

        raise NotImplementedError(f"Configuration type unsupported: {type(config)}")

    def walk_comparisons(
        self,
    ) -> Generator[Union[PreparedComparison, PreparedComparisonException], None, None]:
        """Walk through the prepared comparisons and failures."""
        yield from self.prepare_failures
        yield from self.comparisons

    async def compare(self) -> Result:
        """Run all comparisons and return a combined result."""
        results = []
        for config in self.comparisons:
            if isinstance(config, PreparedComparison):
                results.append(await config.compare())

        if self.prepare_failures:
            result = Result(
                severity=Severity.error,
                reason="At least one configuration failed to initialize",
            )
        else:
            severity = _summarize_result_severity(GroupResultMode.all_, results)
            result = Result(severity=severity)

        self.result = result
        return result


@dataclass
class PreparedGroup(PreparedConfiguration):
    #: The corresponding group from the configuration file.
    config: ConfigurationGroup = field(default_factory=ConfigurationGroup)
    #: The hierarhical parent of this group.  If this is the root group,
    #: 'parent' may be a PreparedFile.
    parent: Optional[Union[PreparedGroup, PreparedFile]] = field(default=None, repr=False)
    #: The configs defined in the group.
    configs: List[AnyPreparedConfiguration] = field(default_factory=list)
    #: The configs that failed to prepare.
    prepare_failures: List[FailedConfiguration] = field(default_factory=list)
    #: Result of all comparisons.
    result: Result = field(default_factory=Result)

    @classmethod
    def from_group(
        cls,
        group: ConfigurationGroup,
        parent: Optional[Union[PreparedGroup, PreparedFile]] = None,
        *,
        client: Optional[happi.Client] = None,
        cache: Optional[DataCache] = None,
    ) -> PreparedGroup:

        if client is None:
            client = happi.Client.from_config()

        if cache is None:
            cache = DataCache()

        prepared = cls(
            cache=cache,
            config=group,
            parent=parent,
            configs=[],
        )

        for config in group.configs:
            prepared_conf = PreparedConfiguration.from_config(
                config=cast(AnyConfiguration, config),
                parent=prepared,
                client=client,
                cache=cache,
            )
            if isinstance(prepared_conf, FailedConfiguration):
                prepared.prepare_failures.append(prepared_conf)
            else:
                prepared.configs.append(prepared_conf)

        return prepared

    @property
    def subgroups(self) -> List[PreparedGroup]:
        """
        Direct descendent subgroups in this group.

        Returns
        -------
        List[PreparedGroup]
        """
        return [
            config
            for config in self.configs
            if isinstance(config, PreparedGroup)
        ]

    def walk_groups(
        self,
    ) -> Generator[Union[PreparedGroup], None, None]:
        """Walk through the prepared groups."""
        for config in self.configs:
            if isinstance(config, PreparedGroup):
                yield config
                yield from config.walk_groups()

    def walk_comparisons(
        self,
    ) -> Generator[Union[PreparedComparison, FailedConfiguration], None, None]:
        """
        Walk through the prepared comparisons.
        """
        yield from self.prepare_failures
        for config in self.configs:
            yield from config.walk_comparisons()

    async def compare(self) -> Result:
        """Run all comparisons and return a combined result."""
        results = []
        for config in self.configs:
            if isinstance(config, PreparedConfiguration):
                results.append(await config.compare())

        if self.prepare_failures:
            result = Result(
                severity=Severity.error,
                reason="At least one configuration failed to initialize",
            )
        else:
            severity = _summarize_result_severity(self.config.mode, results)
            result = Result(
                severity=severity
            )

        self.result = result
        return result


@dataclass
class PreparedDeviceConfiguration(PreparedConfiguration):
    #: The configuration settings.
    config: DeviceConfiguration = field(default_factory=DeviceConfiguration)
    #: The device the comparisons apply to.
    devices: List[ophyd.Device] = field(default_factory=list)
    #: The comparisons to be run on the given devices.
    comparisons: List[PreparedSignalComparison] = field(default_factory=list)
    #: The comparisons that failed to be prepared.
    prepare_failures: List[PreparedComparisonException] = field(default_factory=list)

    @classmethod
    def from_device(
        cls,
        device: Union[ophyd.Device, Sequence[ophyd.Device]],
        by_attr: Dict[str, List[Comparison]],
        shared: Optional[List[Comparison]] = None,
        parent: Optional[PreparedGroup] = None,
        cache: Optional[DataCache] = None,
        client: Optional[happi.Client] = None,
    ) -> PreparedDeviceConfiguration:
        if isinstance(device, Sequence):
            devices = list(device)
        else:
            devices = [device]

        config = cls.from_config(
            DeviceConfiguration(
                devices=[],
                by_attr=by_attr,
                shared=shared or [],
            ),
            additional_devices=devices,
            cache=cache,
            client=client,
            parent=parent,
        )
        return cast(PreparedDeviceConfiguration, config)

    @classmethod
    def from_config(
        cls,
        config: DeviceConfiguration,
        client: Optional[happi.Client] = None,
        parent: Optional[PreparedGroup] = None,
        cache: Optional[DataCache] = None,
        additional_devices: Optional[List[ophyd.Device]] = None,
    ) -> Union[FailedConfiguration, PreparedDeviceConfiguration]:
        if not isinstance(config, DeviceConfiguration):
            raise ValueError(f"Unexpected configuration type: {type(config).__name__}")

        if client is None:
            client = happi.Client.from_config()

        if cache is None:
            cache = DataCache()

        devices = list(additional_devices or [])
        for dev_name in config.devices:
            try:
                devices.append(util.get_happi_device_by_name(dev_name, client=client))
            except Exception as ex:
                return FailedConfiguration(
                    parent=parent,
                    config=config,
                    reason=Result(
                        reason=f"Failed to load happi device: {dev_name}",
                        severity=Severity.error,
                    ),
                    exception=ex,
                )

        prepared_comparisons = []
        prepare_failures = []
        shared = config.shared or []

        prepared = PreparedDeviceConfiguration(
            config=config,
            devices=devices,
            cache=cache,
            parent=parent,
            comparisons=prepared_comparisons,
            prepare_failures=prepare_failures,
        )

        for device in devices:
            for attr, comparisons in config.by_attr.items():
                for comparison in comparisons + shared:
                    try:
                        prepared_comparisons.append(
                            PreparedSignalComparison.from_device(
                                device=device,
                                attr=attr,
                                comparison=comparison,
                                parent=prepared,
                                cache=cache,
                            )
                        )
                    except Exception as ex:
                        prepare_failures.append(ex)

        return prepared


@dataclass
class PreparedPVConfiguration(PreparedConfiguration):
    #: The configuration settings.
    config: PVConfiguration = field(default_factory=PVConfiguration)
    #: The comparisons to be run on the given devices.
    comparisons: List[PreparedSignalComparison] = field(default_factory=list)
    #: The comparisons to be run on the given devices.
    prepare_failures: List[PreparedComparisonException] = field(default_factory=list)

    @classmethod
    def from_pvs(
        cls,
        by_pv: Dict[str, List[Comparison]],
        shared: Optional[List[Comparison]] = None,
        parent: Optional[PreparedGroup] = None,
        cache: Optional[DataCache] = None,
    ) -> PreparedPVConfiguration:
        config = cls.from_config(
            PVConfiguration(
                by_pv=by_pv,
                shared=shared or [],
            ),
            cache=cache,
            parent=parent,
        )
        return cast(PreparedPVConfiguration, config)

    @classmethod
    def from_config(
        cls,
        config: PVConfiguration,
        parent: Optional[PreparedGroup] = None,
        cache: Optional[DataCache] = None,
    ) -> Union[FailedConfiguration, PreparedPVConfiguration]:
        if not isinstance(config, PVConfiguration):
            raise ValueError(f"Unexpected configuration type: {type(config).__name__}")

        if cache is None:
            cache = DataCache()

        prepared_comparisons = []
        prepare_failures = []
        shared = config.shared or []

        prepared = PreparedPVConfiguration(
            config=config,
            cache=cache,
            parent=parent,
            comparisons=prepared_comparisons,
            prepare_failures=prepare_failures,
        )

        for pvname, comparisons in config.by_pv.items():
            for comparison in comparisons + shared:
                try:
                    prepared_comparisons.append(
                        PreparedSignalComparison.from_pvname(
                            pvname=pvname,
                            comparison=comparison,
                            parent=prepared,
                            cache=cache,
                        )
                    )
                except Exception as ex:
                    prepare_failures.append(ex)

        return prepared


@dataclass
class PreparedToolConfiguration(PreparedConfiguration):
    #: The configuration settings.
    config: ToolConfiguration = field(default_factory=ToolConfiguration)
    #: The comparisons to be run on the given devices.
    comparisons: List[PreparedSignalComparison] = field(default_factory=list)
    #: The comparisons that failed to be prepared.
    prepare_failures: List[PreparedComparisonException] = field(default_factory=list)

    @classmethod
    def from_tool(
        cls,
        tool: tools.Tool,
        by_attr: Dict[str, List[Comparison]],
        shared: Optional[List[Comparison]] = None,
        parent: Optional[PreparedGroup] = None,
        cache: Optional[DataCache] = None,
    ) -> PreparedToolConfiguration:
        config = cls.from_config(
            ToolConfiguration(
                tool=tool,
                by_attr=by_attr,
                shared=shared or [],
            ),
            cache=cache,
            parent=parent,
        )
        return cast(PreparedToolConfiguration, config)

    @classmethod
    def from_config(
        cls,
        config: ToolConfiguration,
        parent: Optional[PreparedGroup] = None,
        cache: Optional[DataCache] = None,
    ) -> PreparedToolConfiguration:
        if not isinstance(config, ToolConfiguration):
            raise ValueError(f"Unexpected configuration type: {type(config).__name__}")

        if cache is None:
            cache = DataCache()

        prepared_comparisons = []
        prepare_failures = []
        shared = config.shared or []

        prepared = PreparedToolConfiguration(
            config=config,
            cache=cache,
            parent=parent,
            comparisons=prepared_comparisons,
            prepare_failures=prepare_failures,
        )

        for result_key, comparisons in config.by_attr.items():
            for comparison in comparisons + shared:
                try:
                    prepared_comparisons.append(
                        PreparedToolComparison.from_tool(
                            tool=config.tool,
                            result_key=result_key,
                            comparison=comparison,
                            parent=prepared,
                            cache=cache,
                        )
                    )
                except Exception as ex:
                    prepare_failures.append(ex)

        return prepared


@dataclass
class PreparedComparison:
    """
    A unified representation of comparisons for device signals and standalone PVs.
    """
    #: The data cache to use for the preparation step.
    cache: DataCache = field(repr=False)
    #: The identifier used for the comparison.
    identifier: str = ""
    #: The comparison itself.
    comparison: Comparison = field(default_factory=Comparison)
    #: The name of the associated configuration.
    name: Optional[str] = None
    #: The hierarhical parent of this comparison.
    parent: Optional[PreparedGroup] = field(default=None, repr=False)
    #: The last result of the comparison, if run.
    result: Optional[Result] = None

    async def get_data_async(self) -> Any:
        """
        Get the data according to the comparison's configuration.

        To be immplemented in subclass.

        Returns
        -------
        data : Any
            The acquired data.
        """
        raise NotImplementedError()

    async def _compare(self, data: Any) -> Result:
        """
        Run the comparison.

        To be immplemented in subclass.
        """
        raise NotImplementedError()

    async def compare(self) -> Result:
        """
        Run the comparison and return the Result.

        Returns
        -------
        Result
            The result of the comparison.
        """
        try:
            data = await self.get_data_async()
        except (TimeoutError, asyncio.TimeoutError, ConnectionTimeoutError):
            result = Result(
                severity=self.comparison.if_disconnected,
                reason=f"Unable to retrieve data for comparison: {self.identifier}"
            )
            self.result = result
            return result
        except Exception as ex:
            result = Result(
                severity=Severity.internal_error,
                reason=(
                    f"Getting data for {self.identifier!r} comparison "
                    f"{self.comparison} raised {ex.__class__.__name__}: {ex}"
                ),
            )
            self.result = result
            return result

        self.data = data

        try:
            result = await self._compare(data)
        except Exception as ex:
            result = Result(
                severity=Severity.internal_error,
                reason=(
                    f"Failed to run {self.identifier!r} comparison "
                    f"{self.comparison} raised {ex.__class__.__name__}: {ex} "
                ),
            )

        self.result = result
        return result


@dataclass
class PreparedSignalComparison(PreparedComparison):
    """
    A unified representation of comparisons for device signals and standalone
    PVs.

    Each PreparedSignalComparison has a single leaf in the configuration tree,
    comprised of:
    * A configuration
    * The signal specification.  This is comprised of the configuration and
        "IdentifierAndComparison"
        - DeviceConfiguration: Device and attribute (the "identifier")
        - PVConfiguration: PV name (the "identifier")
    * A comparison to run
        - Including data reduction settings
    """
    #: The hierarhical parent of this comparison.
    parent: Optional[
        Union[PreparedDeviceConfiguration, PreparedPVConfiguration]
    ] = field(default=None, repr=False)
    #: The device the comparison applies to, if applicable.
    device: Optional[ophyd.Device] = None
    #: The signal the comparison is to be run on.
    signal: Optional[ophyd.Signal] = None
    #: The value from the signal the comparison is to be run on.
    data: Optional[Any] = None

    async def get_data_async(self) -> Any:
        """
        Get the provided signal's data from the cache according to the
        reduction configuration.

        Returns
        -------
        data : Any
            The acquired data.

        Raises
        ------
        TimeoutError
            If unable to connect or retrieve data from the signal.
        """
        signal = self.signal
        if signal is None:
            raise ValueError("Signal instance unset")

        data = await self.cache.get_signal_data(
            signal,
            reduce_period=self.comparison.reduce_period,
            reduce_method=self.comparison.reduce_method,
            string=self.comparison.string or False,
        )

        self.data = data
        return data

    async def _compare(self, data: Any) -> Result:
        """
        Run the comparison with the already-acquired data in ``self.data``.
        """
        if data is None:
            # 'None' is likely incompatible with our comparisons and should
            # be raised for separately
            return Result(
                severity=self.comparison.if_disconnected,
                reason=(
                    f"No data available for signal {self.identifier!r} in "
                    f"comparison {self.comparison}"
                ),
            )

        return self.comparison.compare(
            data,
            identifier=self.identifier
        )

    @classmethod
    def from_device(
        cls,
        device: ophyd.Device,
        attr: str,
        comparison: Comparison,
        name: Optional[str] = None,
        parent: Optional[PreparedDeviceConfiguration] = None,
        cache: Optional[DataCache] = None,
    ) -> PreparedSignalComparison:
        """Create a PreparedComparison from a device and comparison."""
        full_attr = f"{device.name}.{attr}"
        logger.debug("Checking %s.%s with comparison %s", full_attr, comparison)
        if cache is None:
            cache = DataCache()

        signal = getattr(device, attr, None)
        if signal is None:
            raise AttributeError(
                f"Attribute {full_attr} does not exist on class "
                f"{type(device).__name__}"
            )

        return cls(
            name=name,
            device=device,
            identifier=full_attr,
            comparison=comparison,
            signal=signal,
            parent=parent,
            cache=cache,
        )

    @classmethod
    def from_pvname(
        cls,
        pvname: str,
        comparison: Comparison,
        name: Optional[str] = None,
        parent: Optional[PreparedPVConfiguration] = None,
        cache: Optional[DataCache] = None,
    ) -> PreparedSignalComparison:
        """Create a PreparedComparison from a PV name and comparison."""
        if cache is None:
            cache = DataCache()

        return cls(
            identifier=pvname,
            device=None,
            signal=cache.signals[pvname],
            comparison=comparison,
            name=name,
            cache=cache,
            parent=parent,
        )


@dataclass
class PreparedToolComparison(PreparedComparison):
    """
    A unified representation of comparisons for device signals and standalone PVs.

    Each PreparedToolComparison has a single leaf in the configuration tree,
    comprised of:
    * A configuration
    * The tool configuration (i.e., a :class:`tools.Tool` instance)
    * Identifiers to compare are dependent on the tool type
    * A comparison to run
        - For example, a :class:`tools.Ping` has keys described in
          :class:`tools.PingResult`.
    """
    #: The device the comparison applies to, if applicable.
    tool: tools.Tool = field(default_factory=lambda: tools.Ping(hosts=[]))

    async def get_data_async(self) -> Any:
        """
        Get the provided tool's result data from the cache.

        Returns
        -------
        data : Any
            The acquired data.
        """
        return await self.cache.get_tool_data(self.tool)

    async def _compare(self, data: Any) -> Result:
        """
        Run the prepared comparison.

        Returns
        -------
        Result
            The result of the comparison.  This is also set in ``self.result``.
        """
        try:
            value = tools.get_result_value_by_key(data, self.identifier)
        except KeyError as ex:
            return Result(
                severity=self.comparison.severity_on_failure,
                reason=(
                    f"Provided key is invalid for tool result {self.tool} "
                    f"{self.identifier!r} ({self.name}): {ex} "
                    f"(in comparison {self.comparison})"
                ),
            )
        return self.comparison.compare(
            value,
            identifier=self.identifier
        )

    @classmethod
    def from_tool(
        cls,
        tool: tools.Tool,
        result_key: str,
        comparison: Comparison,
        name: Optional[str] = None,
        parent: Optional[PreparedToolConfiguration] = None,
        cache: Optional[DataCache] = None,
    ) -> PreparedToolComparison:
        """
        Prepare a tool-based comparison for execution.

        Parameters
        ----------
        tool : Tool
            The tool to run.
        result_key : str
            The key from the result dictionary to check after running the tool.
        comparison : Comparison
            The comparison to perform on the tool's results (looking at the
            specific result_key).
        name : Optional[str], optional
            The name of the comparison.
        path : Optional[List[PathItem]], optional
            The path that led us to this single comparison.
        cache : DataCache, optional
            The data cache to use for this and other similar comparisons.

        Returns
        -------
        PreparedToolComparison
        """
        if cache is None:
            cache = DataCache()
        tool.check_result_key(result_key)
        return cls(
            tool=tool,
            comparison=comparison,
            name=name,
            identifier=result_key,
            cache=cache,
            parent=parent,
        )


AnyPreparedConfiguration = Union[
    PreparedDeviceConfiguration,
    PreparedGroup,
    PreparedPVConfiguration,
    PreparedToolConfiguration
]

_class_to_prepared: Dict[type, type] = {
    ConfigurationFile: PreparedFile,
    ConfigurationGroup: PreparedGroup,
    ToolConfiguration: PreparedToolConfiguration,
    DeviceConfiguration: PreparedDeviceConfiguration,
    PVConfiguration: PreparedPVConfiguration,
}


def get_result_from_comparison(
    item: Union[PreparedComparison, Exception, None]
) -> Tuple[Optional[PreparedComparison], Result]:
    """
    Get a Result, if available, from the provided arguments.

    In the case of an exception (or None/internal error), create one.

    Parameters
    ----------
    item : Union[PreparedComparison, Exception, None]
        The item to grab a result from.

    Returns
    -------
    PreparedComparison or None :
        The prepared comparison, if available
    Result :
        The result instance.
    """
    if item is None:
        return None, Result(
            severity=Severity.internal_error,
            reason="no result available (comparison not run?)"
        )
    if isinstance(item, Exception):
        # An error that was transformed into a Result with a severity
        return None, Result.from_exception(item)

    if item.result is None:
        return item, Result(
            severity=Severity.internal_error,
            reason="no result available (comparison not run?)"
        )

    return item, item.result
