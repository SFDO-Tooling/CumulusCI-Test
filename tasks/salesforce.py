from cumulusci.core.tasks import BaseTask


class TestTask(BaseTask):

    def _run_task(self):
        self.logger.info('running test task')
