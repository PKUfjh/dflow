from copy import deepcopy
from typing import Optional, Dict, List, Union

from .config import config, s3_config
from .io import Inputs, Outputs
from .op_template import OPTemplate
from .step import add_slices
from .task import Task
from .utils import randstr

try:
    from argo.workflows.client import (V1alpha1DAGTemplate, V1alpha1Metadata,
                                       V1alpha1Template)
except Exception:
    pass


class DAG(OPTemplate):
    """
    DAG

    Args:
        name: the name of the dag
        inputs: inputs in the template
        outputs: outputs in the template
        tasks: a list of tasks
        memoize_key: memoized key of the dag
        annotations: annotations for the OP template
        parallelism: maximum number of running pods for the OP template
        """

    def __init__(
            self,
            name: Optional[str] = None,
            inputs: Optional[Inputs] = None,
            outputs: Optional[Outputs] = None,
            tasks: Optional[List[Task]] = None,
            memoize_key: Optional[str] = None,
            annotations: Dict[str, str] = None,
            parallelism: Optional[int] = None,
    ) -> None:
        super().__init__(name=name, inputs=inputs, outputs=outputs,
                         memoize_key=memoize_key, annotations=annotations)
        self.parallelism = parallelism
        self.tasks = []
        if tasks is not None:
            for task in tasks:
                self.add(task)

    @classmethod
    def from_dict(cls, d, templates):
        kwargs = {
            "name": d.get("name", None),
            "inputs": Inputs.from_dict(d.get("inputs", {})),
            "outputs": Outputs.from_dict(d.get("outputs", {})),
            "memoize_key": d.get("memoize", {}).get("key", None),
            "annotations": d.get("metadata", {}).get("annotations", None),
            "parallelism": d.get("parallelism", None),
        }
        tasks = {task["name"]: Task.from_dict(task, templates)
                 for task in d.get("dag", {}).get("tasks", [])}
        for task in tasks.values():
            task.dependencies = [tasks[t] for t in task.dependencies]
        kwargs["tasks"] = list(tasks.values())
        return cls(**kwargs)

    def __iter__(self):
        return iter(self.tasks)

    def add(
            self,
            task: Union[Task, List[Task]],
    ) -> None:
        """
        Add a task or a list of tasks to the dag

        Args:
            task: a task or a list of tasks to be added to the dag
        """
        if not isinstance(task, list):
            task = [task]

        for t in task:
            assert isinstance(t, Task)
            if t.prepare_step is not None:
                self.tasks.append(t.prepare_step)
            self.tasks.append(t)
            if t.check_step is not None:
                self.tasks.append(t.check_step)

    def convert_to_argo(self, memoize_prefix=None,
                        memoize_configmap="dflow", context=None):
        argo_tasks = []
        templates = []
        assert len(self.tasks) > 0, "Dag %s is empty" % self.name
        for task in self.tasks:
            argo_tasks.append(task.convert_to_argo(context))
            templates.append(task.template)

        self.handle_key(memoize_prefix, memoize_configmap)
        argo_template = \
            V1alpha1Template(name=self.name,
                             metadata=V1alpha1Metadata(
                                 annotations=self.annotations),
                             dag=V1alpha1DAGTemplate(
                                 tasks=argo_tasks,
                             ),
                             inputs=self.inputs.convert_to_argo(),
                             outputs=self.outputs.convert_to_argo(),
                             memoize=self.memoize,
                             parallelism=self.parallelism)
        return argo_template, templates

    def resolve(self, pool, futures):
        for task in self.waiting:
            ready = True
            for dep in task.dependencies:
                if dep not in self.finished:
                    ready = False
                    break
            if ready:
                task.phase = "Pending"
                i = self.tasks.index(task)
                future = pool.submit(task.run_with_config, self,
                                     self.context, config, s3_config)
                futures[future] = i
                self.waiting.remove(task)
                self.running.append(task)

    def run(self, workflow_id=None, context=None):
        self.workflow_id = workflow_id
        self.context = context
        from copy import deepcopy
        import concurrent.futures
        pool = concurrent.futures.ProcessPoolExecutor(
            config["debug_pool_workers"])
        futures = {}
        self.waiting = [task for task in self]
        self.running = []
        self.finished = []
        self.resolve(pool, futures)

        while len(self.running) > 0:
            future = next(concurrent.futures.as_completed(futures))
            j = futures.pop(future)
            try:
                t = future.result()
            except Exception:
                import traceback
                traceback.print_exc()
                self.tasks[j].phase = "Failed"
                if not self.tasks[j].continue_on_failed:
                    raise RuntimeError("Task %s failed" % self.tasks[j])
            else:
                self.tasks[j].outputs = deepcopy(t.outputs)
            self.running.remove(self.tasks[j])
            self.finished.append(self.tasks[j])
            self.resolve(pool, futures)

        pool.shutdown()
        assert len(self.finished) == len(self.tasks), "cyclic graph"

    def add_slices(self, slices):
        add_slices(self, slices)

    def copy(self):
        new_template = deepcopy(self)
        new_template.name += "-" + randstr()
        for task, new_task in zip(self.tasks, new_template.tasks):
            new_task.template = task.template
        return new_template
