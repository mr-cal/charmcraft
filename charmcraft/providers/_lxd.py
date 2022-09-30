# Copyright 2021-2022 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# For further info, check https://github.com/canonical/charmcraft

"""Build environment provider support for charmcraft."""

import contextlib
import pathlib
from typing import Generator

from craft_cli import CraftError
from craft_providers import Executor, bases, lxd

from charmcraft import instrum
from charmcraft.config import Base
from charmcraft.env import get_managed_environment_project_path
from charmcraft.utils import confirm_with_user, get_host_architecture

from ._buildd import BASE_CHANNEL_TO_BUILDD_IMAGE_ALIAS, CharmcraftBuilddBaseConfiguration
from ._provider import Provider
from .providers import get_command_environment


class LXDProvider(Provider):
    """Charmcraft's build environment provider.

    :param lxc: Optional lxc client to use.
    :param lxd_project: LXD project to use (default is charmcraft).
    :param lxd_remote: LXD remote to use (default is local).
    """

    def __init__(
        self,
        *,
        lxc: lxd.LXC = lxd.LXC(),
        lxd_project: str = "charmcraft",
        lxd_remote: str = "local",
    ) -> None:
        self.lxc = lxc
        self.lxd_project = lxd_project
        self.lxd_remote = lxd_remote

    @classmethod
    def ensure_provider_is_available(cls) -> None:
        """Ensure provider is available, prompting the user to install it if required.

        :raises CraftError: if provider is not available.
        """
        if not lxd.is_installed():
            if confirm_with_user(
                "LXD is required, but not installed. Do you wish to install LXD "
                "and configure it with the defaults?",
                default=False,
            ):
                try:
                    lxd.install()
                except lxd.LXDInstallationError as error:
                    raise CraftError(
                        "Failed to install LXD. Visit https://snapcraft.io/lxd for "
                        "instructions on how to install the LXD snap for your distribution"
                    ) from error
            else:
                raise CraftError(
                    "LXD is required, but not installed. Visit https://snapcraft.io/lxd for "
                    "instructions on how to install the LXD snap for your distribution"
                )

        try:
            lxd.ensure_lxd_is_ready()
        except lxd.LXDError as error:
            raise CraftError(str(error)) from error

    @classmethod
    def is_provider_available(cls) -> bool:
        """Check if provider is installed and available for use.

        :returns: True if installed.
        """
        return lxd.is_installed()

    def environment(self, *, instance_name: str) -> Executor:
        """Create a bare environment for specified base.

        No initializing, launching, or cleaning up of the environment occurs.

        :param instance_name: Name of the instance.
        """
        return lxd.LXDInstance(
            name=instance_name,
            default_command_environment=get_command_environment(),
            project=self.lxd_project,
            remote=self.lxd_remote,
        )

    @contextlib.contextmanager
    def launched_environment(
        self,
        *,
        charm_name: str,
        project_path: pathlib.Path,
        base: Base,
        bases_index: int,
        build_on_index: int,
    ) -> Generator[Executor, None, None]:
        """Launch environment for specified base.

        :param charm_name: Name of project.
        :param project_path: Path to project.
        :param base: Base to create.
        :param bases_index: Index of `bases:` entry.
        :param build_on_index: Index of `build-on` within bases entry.
        """
        alias = BASE_CHANNEL_TO_BUILDD_IMAGE_ALIAS[base.channel]
        target_arch = get_host_architecture()

        instance_name = self.get_instance_name(
            bases_index=bases_index,
            build_on_index=build_on_index,
            project_name=charm_name,
            project_path=project_path,
            target_arch=target_arch,
        )

        environment = get_command_environment()
        with instrum.Timer("LXD: Configure buildd image"):
            try:
                image_remote = lxd.configure_buildd_image_remote()
            except lxd.LXDError as error:
                raise CraftError(str(error)) from error

        # specify the uid of the owner of the project directory to prevent read-only mounts of
        # the project dir when the currently running user and project dir owner are different
        projectdir_owner_id = project_path.stat().st_uid

        base_configuration = CharmcraftBuilddBaseConfiguration(
            alias=alias, environment=environment, hostname=instance_name
        )
        with instrum.Timer("LXD: Launch"):
            try:
                instance = lxd.launch(
                    name=instance_name,
                    base_configuration=base_configuration,
                    image_name=base.channel,
                    image_remote=image_remote,
                    auto_clean=True,
                    auto_create_project=True,
                    map_user_uid=True,
                    use_snapshots=True,
                    project=self.lxd_project,
                    remote=self.lxd_remote,
                    uid=projectdir_owner_id,
                )
            except (bases.BaseConfigurationError, lxd.LXDError) as error:
                raise CraftError(str(error)) from error

        # Mount project.
        with instrum.Timer("LXD: Mount"):
            instance.mount(host_source=project_path, target=get_managed_environment_project_path())

        try:
            yield instance
        finally:
            # Ensure to unmount everything and stop instance upon completion.
            with instrum.Timer("LXD: Unmount and stop"):
                try:
                    instance.unmount_all()
                    instance.stop()
                except lxd.LXDError as error:
                    raise CraftError(str(error)) from error
