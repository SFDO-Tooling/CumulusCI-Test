from cumulusci.core.tasks import BaseTask


class ExampleTask(BaseTask):
    def _run_task(self):
        self.logger.info("Called _run_task")


class StaticPreflightTask(BaseTask):

    task_options = {
        "task_name": {
            "description": "Task that this preflight is for",
            "required": True,
        },
        "status_code": {
            "description": 'Status code to return, default to "ok".',
            "required": False,
        },
        "msg": {"description": "Message to be included", "required": False},
    }

    def _run_task(self):
        self.return_values["status_code"] = self.options.get("status_code", "ok")
        self.return_values["task_name"] = self.options.get("task_name")
        self.return_values["msg"] = self.options.get("msg", "")
