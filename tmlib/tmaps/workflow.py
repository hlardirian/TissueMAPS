import os
import re
import logging
import importlib
from gc3libs import Run
from gc3libs.workflow import SequentialTaskCollection
from gc3libs.workflow import AbortOnError
from ..readers import WorkflowDescriptionReader
from ..cluster import BasicClusterRoutines
from ..errors import WorkflowNextStepError
from .. import utils

logger = logging.getLogger(__name__)


class ClusterWorkflowManager(SequentialTaskCollection, AbortOnError):

    '''
    Class for reading workflow descriptions from a YAML file.
    '''

    def __init__(self, experiment, virtualenv, verbosity):
        '''
        Instantiate an instance of class ClusterWorkflowManager.

        Parameters
        ----------
        experiment: str
            configured experiment object
        virtualenv: str
            name of a virtual environment that needs to be activated
        verbosity: int
            logging level verbosity

        Returns
        -------
        ClusterWorkflowManager
        '''
        super(ClusterWorkflowManager, self).__init__(
            tasks=None, jobname='tmaps')
        self.experiment = experiment
        self.virtualenv = virtualenv
        self.verbosity = verbosity
        # Forcing StagedTaskCollection to stop inside next()
        self.__stopped = False
        self.tasks = list()
        self.expected_outputs = list()
        self._add_step(0)

    @property
    def workflow_file(self):
        '''
        Returns
        -------
        str
            name of the file that describes the workflow

        Note
        ----
        The file is located in the root directory of the experiment folder.
        '''
        self._workflow_file = '{experiment}.workflow'.format(
                                    experiment=self.experiment.name)
        return self._workflow_file

    @property
    def workflow_description(self):
        '''
        Returns
        -------
        List[str]
            commands for each individual step of the workflow
        '''
        with WorkflowDescriptionReader(self.experiment.dir) as reader:
            workflow = reader.read(self.workflow_file)
        self._workflow_description = [
            step.format(experiment_dir=self.experiment.dir).split()
            for step in workflow
        ]
        return self._workflow_description

    def _create_jobs_for_step(self, step_desciption):
        package_name = 'tmlib'
        prog_name = step_desciption[0]
        argparser_module_name = '%s.%s.argparser' % (package_name, prog_name)
        logger.debug('load module "%s"' % argparser_module_name)
        argparser_module = importlib.import_module(argparser_module_name)
        parser = argparser_module.parser
        parser.prog = prog_name
        cli_module_name = '%s.%s.cli' % (package_name, prog_name)
        logger.debug('load module "%s"' % cli_module_name)
        cli_module = importlib.import_module(cli_module_name)

        init_command = step_desciption[1:]
        # TODO: add additional arguments using the format string method
        # with a dictionary read from the user.cfg file
        args = parser.parse_args(init_command)
        cli_class_inst = getattr(cli_module, prog_name.capitalize())(args)

        # Check whether inputs of current step were produced upstream
        if not all([
                os.path.exists(i) for i in cli_class_inst.required_inputs
            ]):
            logger.error('required inputs were not produced upstream')
            raise WorkflowNextStepError('required inputs do not exist')

        # Create job_descriptions for new step
        getattr(cli_class_inst, args.method_name)()

        # Store the expected outputs to be later able to check whether they
        # were actually generated
        self.expected_outputs.append(cli_class_inst.expected_outputs)

        # Take the base of the "init" command to get the positional arguments
        # of the main program parser and extend it with additional
        # arguments required for submit subparser
        submit_command = list()
        if self.verbosity > 0:
            submit_command.append('-v')
        submit_command.extend(init_command[:init_command.index('init')])
        submit_command.append('submit')
        if self.virtualenv:
            submit_command.extend(['--virtualenv', self.virtualenv])
        args = parser.parse_args(submit_command)
        cli_class_inst = getattr(cli_module, prog_name.capitalize())(args)
        return cli_class_inst.jobs

    def _add_step(self, index):
        if (index > 0
                and not all([
                    os.path.exists(f) for f in self.expected_outputs[-1]
                ])):
            logger.error('expected outputs were not generated')
            raise WorkflowNextStepError('outputs of previous step do not exist')
        step_desciption = self.workflow_description[index]
        task = self._create_jobs_for_step(step_desciption)
        self.tasks.append(task)

    def next(self, done):
        '''
        Progress to the next step of the workflow.

        Parameters
        ----------
        done: int
            zero-based index of the last processed step

        Returns
        -------
        gc3libs.Run.State
        '''
        # TODO: resubmission and all the related shizzle
        if done+1 < len(self.workflow_description):
            try:
                self._add_step(done+1)
            except Exception as error:
                logger.error('adding next step failed: %s' % str(error))
                self.exitcode = 0
                logger.debug('exitcode set to zero')
                if self.__stopped:
                    return Run.State.STOPPED
                else:
                    return Run.State.TERMINATED
        state = super(ClusterWorkflowManager, self).next(done)
        if state == Run.State.TERMINATED:
            self.exitcode = 0
            if self.__stopped:
                return Run.State.STOPPED
        return state


class WorkflowClusterRoutines(BasicClusterRoutines):

    def __init__(self, experiment, prog_name):
        '''
        Instantiate an instance of class WorkflowClusterRoutines.

        Parameters
        ----------
        experiment: Experiment
            configured experiment object
        prog_name: str
            name of the corresponding program (command line interface)
        '''
        super(WorkflowClusterRoutines, self).__init__(experiment)
        self.experiment = experiment
        self.prog_name = prog_name

    @property
    def project_dir(self):
        '''
        Returns
        -------
        str
            directory where *.job* files and log output will be stored
        '''
        self._project_dir = os.path.join(self.experiment.dir, 'tmaps')
        if not os.path.exists(self._project_dir):
            logging.debug('create project directory: %s' % self._project_dir)
            os.mkdir(self._project_dir)
        return self._project_dir
