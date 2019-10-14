import base64
import hashlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from cumulusci.core.config import TaskConfig
from cumulusci.core.exceptions import CumulusCIFailure
from cumulusci.core.exceptions import TaskOptionsError
from cumulusci.core.flowrunner import FlowCoordinator
from cumulusci.core.utils import process_bool_arg
from cumulusci.salesforce_api.utils import get_simple_salesforce_connection
from cumulusci.tasks.salesforce import BaseSalesforceApiTask
from cumulusci.tasks.salesforce import Deploy
from cumulusci.tasks.sfdx import SFDXBaseTask
from cumulusci.utils import cd
from cumulusci.utils import download_extract_zip
from cumulusci.utils import download_extract_github

# from cumulusci.utils import temporary_dir
from cumulusci.utils import zip_inject_namespace
from cumulusci.utils import zip_strip_namespace
from cumulusci.utils import zip_tokenize_namespace


class DependencyLookupError(CumulusCIFailure):
    pass


class PackageUploadFailure(CumulusCIFailure):
    pass


# FIXME: Move to cumulusci.utils
@contextmanager
def temporary_dir(chdir=True):
    """Context manager that creates a temporary directory and chdirs to it.

    When the context manager exits it returns to the previous cwd
    and deletes the temporary directory.
    """
    d = tempfile.mkdtemp()
    try:
        if chdir is True:
            with cd(d):
                yield d
        else:
            yield d
    finally:
        if os.path.exists(d):
            shutil.rmtree(d)


class CreatePackageVersion(Deploy, BaseSalesforceApiTask):
    task_options = {
        "package": {
            "description": "The key for the package specified under packages in the cumulusci.yml",
            "required": True,
        },
        "version_type": {
            "description": "The part of the version number to increment.  Options are major, minor, patch.  Defaults to minor"
        },
        "dependency_org": {
            "description": "The org name of the org to use for project dependencies lookup. If not provided, a scratch org will be created with the org name 2gp_dependencies."
        },
        "force_create": {
            "description": "If set to True, a new package version will be created even if an existing version is found with the same metadata"
        },
        "force_create_dependencies": {
            "description": "If set to True, a new package version will be created for dependencies even if an existing version is found with the same metadata"
        },
    }

    # hooks for tests
    _download_extract_github = staticmethod(download_extract_github)
    _download_extract_zip = staticmethod(download_extract_zip)

    def _init_options(self, kwargs):
        super()._init_options(kwargs)

        if (
            self.options.get("package")
            and self.options["package"] not in self.project_config.packages.keys()
        ):
            raise TaskOptionsError(
                "The package {} could not be found in the cumulusci.yml file".format(
                    self.options["package"]
                )
            )
        else:
            self.package_config = self.project_config.packages.get(
                self.options["package"]
            )
        if self.package_config["package_type"] not in ("unlocked", "managed"):
            raise TaskOptionsError(
                "Cannot upload package of type {package_type}".format(
                    **self.package_config
                )
            )
        self.options["force_create"] = process_bool_arg(
            self.options.get("force_create", False)
        )
        self.options["force_create_dependencies"] = process_bool_arg(
            self.options.get("force_create_dependencies", False)
        )
        if "namespace" not in self.package_config:
            self.package_config[
                "namespace"
            ] = self.project_config.project__package__namespace
        if "version_type" not in self.options:
            self.options["version_type"] = "minor"

    def _run_task(self):
        self.package_id = self._get_or_create_package(self.package_config)
        self.return_values["package_id"] = self.package_id

        self.request_id = self._create_version_request(
            self.package_id, self.package_config
        )
        self.return_values["request_id"] = self.request_id

        self._poll()

        # Get the Package2Version
        res = self.tooling.query(
            "SELECT MajorVersion, MinorVersion, PatchVersion, BuildNumber, SubscriberPackageVersionId FROM Package2Version WHERE Id='{}' ".format(
                self.package_version_id
            )
        )
        package2_version = res["records"][0]

        # Get the SubscriberPackageVersion
        res = self.tooling.query(
            "SELECT Dependencies FROM SubscriberPackageVersion WHERE Id='{SubscriberPackageVersionId}' ".format(
                **package2_version
            )
        )
        subscriber_version = res["records"][0]
        self.return_values["version_number"] = self._get_version_number(
            package2_version
        )
        # TODO: Update the version_name for the package using jinja2 formatting now that we know the real version number
        self.return_values["package2_version_id"] = self.package_version_id
        self.return_values["subscriber_package_version_id"] = package2_version[
            "SubscriberPackageVersionId"
        ]
        self.return_values["dependencies"] = subscriber_version["Dependencies"]
        self.logger.info("Return Values: {}".format(self.return_values))

    def _poll_action(self):
        res = self.tooling.query(
            "SELECT Id, Status, Package2VersionId FROM Package2VersionCreateRequest WHERE Id = '{}'".format(
                self.request_id
            )
        )
        request = res["records"][0]
        if request["Status"] == "Success":
            self.logger.info(
                "[{Status}]: Package creation successful".format(**request)
            )
            self.poll_complete = True
            self.request = request
            self.package_version_id = request["Package2VersionId"]
            return
        elif request["Status"] == "Error":
            self.logger.error(
                "[{Status}]: Package creation failed with error:".format(**request)
            )
            res = self.tooling.query(
                "SELECT Message FROM Package2VersionCreateRequestError WHERE ParentRequestId = '{}'".format(
                    request["Id"]
                )
            )
            errors = []
            if res["size"] > 0:
                for error in res["records"]:
                    errors.append(error["Message"])
                    self.logger.error(error["Message"])
            raise PackageUploadFailure("\n".join(errors))
        elif request["Status"] in ("Queued", "InProgress"):
            self.logger.info(
                "[{Status}]: Checking status of Package2VersionCreateRequest {Id}".format(
                    **request
                )
            )
            return

    def _get_or_create_package(self, package_config):
        query = "SELECT Id FROM Package2 WHERE IsDeprecated = FALSE AND Name='{name}'".format(
            **package_config
        )
        if package_config.get("namespace"):
            query += " AND NamespacePrefix='{namespace}'".format(**package_config)
        else:
            query += " AND NamespacePrefix=null"
        res = self.tooling.query(query)
        if res["size"] > 1:
            raise TaskOptionsError(
                "Found {size} packages with the same name and namespace"
            )
        if res["size"] == 1:
            return res["records"][0]["Id"]

        self.logger.info("No existing package found, creating the package")
        Package2 = self._get_tooling_object("Package2")
        package = Package2.create(
            {
                "ContainerOptions": package_config["package_type"],
                "Name": package_config["name"],
                "NamespacePrefix": package_config["namespace"],
            }
        )
        return package["id"]

    def _get_version_number(self, version):
        version_number = "{MajorVersion}.{MinorVersion}.{PatchVersion}.{BuildNumber}".format(
            **version
        )
        return version_number

    def _get_next_version_number(self, package_id):
        res = self.tooling.query(
            "SELECT "
            "MajorVersion, "
            "MinorVersion, "
            "PatchVersion, "
            "BuildNumber, "
            "IsReleased "
            "FROM Package2Version "
            "WHERE Package2Id='{}' "
            "ORDER BY MajorVersion DESC, "
            "MinorVersion DESC, "
            "PatchVersion DESC, "
            "BuildNumber DESC "
            "LIMIT 1".format(package_id)
        )
        if res["size"] == 0:
            version_parts = {
                "MajorVersion": 1 if self.options["version_type"] == "major" else 0,
                "MinorVersion": 1 if self.options["version_type"] == "minor" else 0,
                "PatchVersion": 1 if self.options["version_type"] == "patch" else 0,
                "BuildNumber": "NEXT",
            }
            return self._get_version_number(version_parts)
        last_version = res["records"][0]
        version_parts = {
            "MajorVersion": last_version["MajorVersion"],
            "MinorVersion": last_version["MinorVersion"],
            "PatchVersion": last_version["PatchVersion"],
            "BuildNumber": "NEXT",
        }
        if last_version["IsReleased"] is True:
            version_parts[self.options["version_type"]] += 1
            if self.options["version_type"] == "major":
                version_parts["MajorVersion"] += 1
                version_parts["MinorVersion"] = 0
            if self.options["version_type"] == "minor":
                version_parts["MinorVersion"] += 1
            if self.options["version_type"] in ("major", "minor"):
                version_parts["PatchVersion"] = 0
            if self.options["version_type"] == "patch":
                version_parts["PatchVersion"] += 1
            version_parts["BuildNumber"] = "NEXT"
            return self._get_version_number(version_parts)
        else:
            version_parts["BuildNumber"] = "NEXT"
            return self._get_version_number(version_parts)

    def _add_files_to_package(self, package_zip, path):
        with cd(path):
            for file_to_package in self._get_files_to_package():
                package_zip.write(file_to_package)

    def _has_managed_project_dependency(self, project_dependencies):
        for dependency in project_dependencies:
            if "namespace" in dependency:
                return True
            if "dependencies" in dependency:
                if self._has_managed_project_dependency(dependency["dependencies"]):
                    return True
        return False

    def _get_dependency_org(self):
        if self.options.get("dependency_org"):
            org_name = self.options["dependency_org"]
            org = self.project_config.keychain.get_org(self.options["dependency_org"])
        else:
            org_name = "2gp_dependencies"
            if org_name not in self.project_config.keychain.list_orgs():
                self.project_config.keychain.create_scratch_org(
                    "2gp_dependencies", "dev"
                )

            org = self.project_config.keychain.get_org("2gp_dependencies")
            if org.created and org.expired:
                self.logger.info(
                    "Recreating expired scratch org named 2gp_dependencies to resolve package dependencies"
                )
                org.create_org()
                self.project_config.keychain.set_org("2gp_dependencies", org)
            if org.created:
                self.logger.info(
                    "Using existing scratch org named 2gp_dependencies to resolve dependencies"
                )
            else:
                self.logger.info(
                    "A new scratch org with the name 2gp_dependencies will be created to resolve dependencies"
                )

            self.logger.info(
                "Running the dependencies flow against the 2gp_dependencies scratch org"
            )
            coordinator = FlowCoordinator(
                self.project_config, self.project_config.get_flow("dependencies")
            )
            coordinator.run(org)

        return org

    def _get_installed_dependencies(self, org):
        org_tooling = get_simple_salesforce_connection(
            self.project_config, org, api_version="41.0"
        )
        org_tooling.base_url += "tooling/"

        self.logger.info(
            "Querying installed package version ids in org {}".format(org.name)
        )
        installed_versions = org_tooling.query(
            "SELECT "
            "SubscriberPackage.Id, "
            "SubscriberPackage.Name, "
            "SubscriberPackage.NamespacePrefix, "
            "SubscriberPackageVersion.Id, "
            "SubscriberPackageVersion.Name, "
            "SubscriberPackageVersion.MajorVersion, "
            "SubscriberPackageVersion.MinorVersion, "
            "SubscriberPackageVersion.PatchVersion, "
            "SubscriberPackageVersion.BuildNumber, "
            "SubscriberPackageVersion.IsBeta, "
            "SubscriberPackageVersion.IsManaged "
            "FROM InstalledSubscriberPackage"
        )

        installed_dependencies = {}
        if installed_versions["size"] > 0:
            for installed in installed_versions["records"]:
                if installed["SubscriberPackage"]["NamespacePrefix"] is None:
                    continue
                version_str = "{MajorVersion}.{MinorVersion}".format(
                    **installed["SubscriberPackageVersion"]
                )
                if installed["SubscriberPackageVersion"]["PatchVersion"]:
                    version_str += ".{PatchVersion}".format(
                        installed["SubscriberPackageVersion"]
                    )
                if installed["SubscriberPackageVersion"]["IsBeta"]:
                    version_str += " (Beta {BuildNumber}".format(
                        installed["SubscriberPackageVersion"]
                    )

                installed_dependencies[
                    installed["SubscriberPackage"]["NamespacePrefix"]
                    + "@"
                    + version_str
                ] = {
                    "package_id": installed["SubscriberPackage"]["Id"],
                    "package_name": installed["SubscriberPackage"]["Name"],
                    "version_id": installed["SubscriberPackageVersion"]["Id"],
                    "version_name": installed["SubscriberPackageVersion"]["Name"],
                }
        return installed_dependencies

    def _convert_project_dependencies(self, project_dependencies, installed_dependencies):
        dependencies = []
        for dependency in project_dependencies:
            dependency_info = {}
            if dependency.get("namespace"):
                version_info = installed_dependencies.get(
                    "{namespace}@{version}".format(**dependency)
                )
                if not version_info:
                    raise DependencyLookupError(
                        "Could not find installed dependency in org {}: {namespace}@{version}".format(
                            org_name, **dependency
                        )
                    )
                self.logger.info(
                    "Adding dependency {}@{} with id {}".format(
                        dependency["namespace"],
                        dependency["version"],
                        version_info["version_id"],
                    )
                )
                dependency_info["subscriberPackageVersionId"] = version_info["version_id"]
                
            if dependency.get("repo_name"):
                if dependency.get("subfolder", "").startswith("unpackaged/post"):
                    continue
                version_id = self._create_package_from_github(dependency)
                self.logger.info(
                    "Adding dependency {}/{} {} with id {}".format(
                        dependency["repo_owner"],
                        dependency["repo_name"],
                        dependency["subfolder"],
                        version_id,
                    )
                )
                dependency_info["subscriberPackageVersionId"] = version_id

            if dependency.get("dependencies"):
                dependencies.extend(
                    self._convert_project_dependencies(
                        dependency["dependencies"], installed_dependencies
                    )
                )
                
            dependencies.append(dependency_info)

        return dependencies

    def _get_unpackaged_pre_dependencies(self, dependencies):
        path = "unpackaged/pre"
        for item in sorted(os.listdir(path)):
            item_path = os.path.join(path, item)
            if not os.path.isdir(item_path):
                continue
            version_id = self._create_package_from_local(item_path)
            self.logger.info(
                "Adding dependency {}/{} {} with id {}".format(
                    self.project_config.repo_owner,
                    self.project_config.repo_name,
                    item_path,
                    version_id,
                )
            )
            dependencies.append({"subscriberPackageVersionId": version_id})

        return dependencies

    def _get_dependencies(self):
        dependencies = []

        project_dependencies = self.project_config.get_static_dependencies()

        lookup_version_ids = self._has_managed_project_dependency(project_dependencies)

        if project_dependencies and lookup_version_ids:
            org = self._get_dependency_org()
            installed_dependencies = self._get_installed_dependencies(org)
            dependencies.extend(self._convert_project_dependencies(project_dependencies, installed_dependencies))

        dependencies = self._get_unpackaged_pre_dependencies(dependencies)
        return dependencies

    def _create_package_from_github(self, dependency):
        # FIXME: This is yanked and slightly modified from UpdateDependencies and should be refactored out to somewhere reusable between both tasks
        gh_for_repo = self.project_config.get_github_api(
            dependency["repo_owner"], dependency["repo_name"]
        )
        package_zip = self._download_extract_github(
            gh_for_repo,
            dependency["repo_owner"],
            dependency["repo_name"],
            dependency["subfolder"],
            ref=dependency.get("ref"),
        )

        if dependency.get("namespace_tokenize"):
            self.logger.info(
                "Replacing namespace prefix {}__ in files and filenames with namespace token strings".format(
                    "{}__".format(dependency["namespace_tokenize"])
                )
            )
            package_zip = zip_tokenize_namespace(
                package_zip,
                namespace=dependency["namespace_tokenize"],
                logger=self.logger,
            )

        if dependency.get("namespace_inject"):
            self.logger.info(
                "Replacing namespace tokens with {}".format(
                    "{}__".format(dependency["namespace_inject"])
                )
            )
            package_zip = zip_inject_namespace(
                package_zip,
                namespace=dependency["namespace_inject"],
                managed=not dependency.get("unmanaged"),
                namespaced_org=self.options["namespaced_org"],
                logger=self.logger,
            )

        if dependency.get("namespace_strip"):
            self.logger.info(
                "Removing namespace prefix {}__ from all files and filenames".format(
                    "{}__".format(dependency["namespace_strip"])
                )
            )
            package_zip = zip_strip_namespace(
                package_zip, namespace=dependency["namespace_strip"], logger=self.logger
            )

        with temporary_dir() as path:
            with cd(path):
                package_zip.extractall(path)
                package_config = {
                    "name": "{repo_owner}/{repo_name} {subfolder}".format(**dependency),
                    "version_name": "{repo_owner}/{repo_name} {subfolder} - ".format(
                        **dependency
                    )
                    + "{{ version }}",
                    "package_type": "unlocked",
                    "path": os.path.join(path),
                    # FIXME: Ideally we'd do this without a namespace but that causes package creation errors
                    "namespace": self.package_config.get("namespace"),
                }
                package_id = self._get_or_create_package(package_config)
                self.request_id = self._create_version_request(
                    package_id, package_config
                )

        self._poll()
        res = self.tooling.query(
            "SELECT "
            "MajorVersion, "
            "MinorVersion, "
            "PatchVersion, "
            "BuildNumber, "
            "SubscriberPackageVersionId "
            "FROM Package2Version "
            "WHERE Id='{}' ".format(self.package_version_id)
        )
        package2_version = res["records"][0]

        return package2_version["SubscriberPackageVersionId"]

    def _create_package_from_local(self, path):
        self.logger.info("Creating package for dependencies in {}".format(path))
        package_name = "{}/{} {}".format(
            self.project_config.repo_owner, self.project_config.repo_name, path
        )
        package_config = {
            "name": package_name,
            "version_name": package_name + "{{ version }}",
            "package_type": "unlocked",
            "path": path,
            # FIXME: Ideally we'd do this without a namespace but that causes package creation errors
            "namespace": self.package_config.get("namespace"),
        }
        package_id = self._get_or_create_package(package_config)
        self.request_id = self._create_version_request(package_id, package_config)
        self._poll()
        self.poll_complete = False
        res = self.tooling.query(
            "SELECT "
            "MajorVersion, "
            "MinorVersion, "
            "PatchVersion, "
            "BuildNumber, "
            "SubscriberPackageVersionId "
            "FROM Package2Version "
            "WHERE Id='{}' ".format(self.package_version_id)
        )
        package2_version = res["records"][0]
        return package2_version["SubscriberPackageVersionId"]

    def _create_version_request(self, package_id, package_config):
        # Prepare the version_info file
        version_bytes = io.BytesIO()
        version_info = zipfile.ZipFile(version_bytes, "w", zipfile.ZIP_DEFLATED)

        # Zip up the packaged metadata
        package_bytes = io.BytesIO()
        package_zip = zipfile.ZipFile(package_bytes, "w", zipfile.ZIP_DEFLATED)

        if package_config.get("source_format") == "sfdx":
            self.logger.info("Converting from sfdx to mdapi format")
            with temporary_dir(chdir=False) as path:
                task_config = TaskConfig(
                    {
                        "options": {
                            "command": "force:source:convert -d {} -r {path} -n '{name}'".format(
                                path, **package_config
                            )
                        }
                    }
                )
                self.logger.info("cwd: {}".format(os.getcwd()))
                task = SFDXBaseTask(self.project_config, task_config)
                task()
                self._add_files_to_package(package_zip, path)
        else:
            self._add_files_to_package(package_zip, package_config["path"])

        package_zip.close()
        package_zip_processed = self._process_zip_file(zipfile.ZipFile(package_bytes))
        package_zip_processed.close()

        # Add the package.zip to version_info
        version_info.writestr("package.zip", package_bytes.getvalue())

        # Get an md5 hash of the package.zip file
        package_hash = hashlib.blake2b(package_bytes.getvalue()).hexdigest()

        is_dependency = package_config != self.package_config

        # Check for an existing package with the same contents
        if (is_dependency and self.options["force_create_dependencies"] is False) or (
            self.options["force_create"] is False
        ):
            res = self.tooling.query(
                "SELECT Id "
                "FROM Package2VersionCreateRequest "
                "WHERE Package2Id = '{}' "
                "AND Status != 'Error' "
                "AND Tag = 'hash:{}'".format(package_id, package_hash)
            )
            if res["size"] > 0:
                self.logger.info(
                    "Found existing request for package with the same metadata.  Using existing package."
                )
                return res["records"][0]["Id"]

        # Create the package2-descriptor.json contents and write to version_info
        version_number = self._get_next_version_number(package_id)
        package_descriptor_info = {
            "ancestorId": "",
            "id": package_id,
            "path": package_config["path"],
            "versionName": package_config["version_name"],
            "versionNumber": version_number,
        }

        # Get the dependencies for the package
        if (
            is_dependency is False
            and self.package_config.get("dependencies") == "project"
        ):
            self.logger.info("Determining dependencies for package")
            dependencies = self._get_dependencies()
            if dependencies:
                package_descriptor_info["dependencies"] = dependencies

        version_info.writestr(
            "package2-descriptor.json", json.dumps(package_descriptor_info)
        )
        fp = version_info.fp
        version_info.close()
        version_info = base64.b64encode(fp.getvalue()).decode("utf-8")

        Package2CreateVersionRequest = self._get_tooling_object(
            "Package2VersionCreateRequest"
        )
        request_info = {
            "Branch": package_config.get("branch") or self.project_config.git_branch,
            "Package2Id": package_id,
            "Tag": package_config.get("tag") or "hash:{}".format(package_hash),
            "VersionInfo": version_info,
        }

        request = Package2CreateVersionRequest.create(request_info)
        return request["id"]
