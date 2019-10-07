import base64
import io
import json
import zipfile
from cumulusci.core.exceptions import CumulusCIFailure
from cumulusci.core.exceptions import TaskOptionsError
from cumulusci.tasks.salesforce import BaseSalesforceApiTask
from cumulusci.tasks.salesforce import Deploy
from cumulusci.utils import cd


class CreatePackageVersion(Deploy, BaseSalesforceApiTask):
    task_options = {
        "package": {
            "description": "The key for the package specified under project -> packages in the cumulusci.yml",
            "required": True,
        },
        "version_type": {
            "description": "The part of the version number to increment.  Options are major, minor, patch.  Defaults to minor",
            "required": False,
        },
    }

    def _init_options(self, kwargs):
        super()._init_options(kwargs)

        if (
            self.options.get("package")
            and self.options["package"]
            not in self.project_config.project__packages.keys()
        ):
            raise TaskOptionsError(
                "The package {} could not be found in the cumulusci.yml file".format(
                    self.options["package"]
                )
            )
        else:
            self.package_config = self.project_config.project__packages.get(
                self.options["package"]
            )
        if self.package_config["package_type"] not in ("unlocked", "managed"):
            raise TaskOptionsError(
                "Cannot upload package of type {package_type}".format(
                    **self.package_config
                )
            )
        if "namespace" not in self.package_config:
            self.package_config[
                "namespace"
            ] = self.project_config.project__package__namespace
        if "version_type" not in self.options:
            self.options["version_type"] = "minor"

    def _run_task(self):
        self.package_id = self._get_or_create_package()
        self.logger.info("PackageId: {}".format(self.package_id))
        self.return_values["package_id"] = self.package_id

        self.request_id = self._create_version_request(self.package_id)
        self.logger.info("Package2VersionCreateRequestId: {}".format(self.request_id))
        self.return_values["request_id"] = self.request_id

        self._poll()
        self.logger.info("Package2VersionId: {}".format(self.package_version_id))

        self.return_values["package_version_id"] = self.package_version_id

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

    def _get_or_create_package(self):
        res = self.tooling.query(
            "SELECT Id FROM Package2 WHERE IsDeprecated = FALSE AND Name='{name}' AND NamespacePrefix='{namespace}'".format(
                **self.package_config
            )
        )
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
                "ContainerOptions": self.package_config["package_type"],
                "Name": self.package_config["name"],
                "NamespacePrefix": self.package_config["namespace"],
            }
        )
        return package["id"]

    def _add_version_number(self, version_parts):
        version = "{major}.{minor}".format(**version_parts)
        if version_parts["patch"] > 0:
            version_str += ".{patch}".format(**version_parts)
        version_parts["version"] = version + " (Beta {})".format(
            version_parts["build"] + 1
        )
        version_parts["version_number"] = "{major}.{minor}.{patch}.{build}".format(
            **version_parts
        )
        return version_parts

    def _get_version_parts(self, package_id):
        res = self.tooling.query(
            "SELECT MajorVersion, MinorVersion, PatchVersion, BuildNumber, IsReleased FROM Package2Version WHERE Package2Id='{}' ORDER BY MajorVersion DESC, MinorVersion DESC, PatchVersion DESC, BuildNumber DESC LIMIT 1".format(
                package_id
            )
        )
        if res["size"] == 0:
            return self._add_version_number(
                {"major": 1, "minor": 0, "patch": 0, "build": 0}
            )
        version = res["records"][0]
        version_parts = {
            "major": version["MajorVersion"],
            "minor": version["MajorVersion"],
            "patch": version["PatchVersion"],
            "build": version["BuildNumber"],
        }
        if version["IsReleased"] is True:
            version_parts[self.options["version_type"]] += 1
            if self.options["version_type"] == "major":
                version_parts["minor"] = 0
            if self.options["version_type"] in ("major", "minor"):
                version_parts["patch"] = 0
            version_parts["build"] = 0
            return self._add_version_number(version_parts)
        else:
            version_parts["build"] += 1
            return self._add_version_number(version_parts)

    def _create_version_request(self, package_id):
        # Prepare the version_info file
        version_bytes = io.BytesIO()
        version_info = zipfile.ZipFile(version_bytes, "w", zipfile.ZIP_DEFLATED)

        # Zip up the packaged metadata
        package_bytes = io.BytesIO()
        package_zip = zipfile.ZipFile(package_bytes, "w", zipfile.ZIP_DEFLATED)
        with cd(self.package_config["path"]):
            for file_to_package in self._get_files_to_package():
                package_zip.write(file_to_package)
        package_zip.close()
        package_zip_processed = self._process_zip_file(zipfile.ZipFile(package_bytes))
        package_zip_processed.close()

        # Add the package.zip to version_info
        version_info.writestr("package.zip", package_bytes.getvalue())

        # Create the package2-descriptor.json contents and write to version_info
        version_parts = self._get_version_parts(package_id)
        package_descriptor_info = {
            "ancestorId": "",
            "id": package_id,
            "path": self.package_config["path"],
            "versionName": self.package_config["version_name"],
            "versionNumber": version_parts["version_number"],
        }
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
            "Branch": self.package_config.get("branch"),
            "Package2Id": package_id,
            "Tag": self.package_config.get("branch"),
            "VersionInfo": version_info,
        }

        request = Package2CreateVersionRequest.create(request_info)
        return request["id"]
