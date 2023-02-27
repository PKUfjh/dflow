import json
import logging
import os
import tempfile
from collections import UserDict, UserList
from copy import deepcopy
from typing import Any, List, Union

import jsonpickle

from .config import config, s3_config
from .io import S3Artifact
from .op_template import get_k8s_core_v1_api
from .utils import (download_artifact, download_s3, get_key, upload_artifact,
                    upload_s3)

logger = logging.getLogger(__name__)


class ArgoObjectDict(UserDict):
    """
    Generate ArgoObjectDict and ArgoObjectList on initialization rather than
    on __getattr__, otherwise modify a.b.c will not take effect
    """

    def __init__(self, d):
        super().__init__(d)
        for key, value in self.items():
            if isinstance(value, dict):
                self.data[key] = ArgoObjectDict(value)
            elif isinstance(value, list):
                self.data[key] = ArgoObjectList(value)

    def __getattr__(self, key):
        if key == "data":
            return super().__getattr__(key)

        if key in self.data:
            return self.data[key]
        else:
            raise AttributeError(
                "'ArgoObjectDict' object has no attribute '%s'" % key)

    def __setattr__(self, key, value):
        if key == "data":
            return super().__setattr__(key, value)

        self.data[key] = value

    def recover(self):
        return {key: value.recover() if isinstance(value, (ArgoObjectDict,
                                                           ArgoObjectList))
                else value for key, value in self.data.items()}


class ArgoObjectList(UserList):
    def __init__(self, li):
        super().__init__(li)
        for i, value in enumerate(self.data):
            if isinstance(value, dict):
                self.data[i] = ArgoObjectDict(value)
            elif isinstance(value, list):
                self.data[i] = ArgoObjectList(value)

    def recover(self):
        return [value.recover() if isinstance(value, (ArgoObjectDict,
                                                      ArgoObjectList))
                else value for value in self.data]


class ArgoParameter(ArgoObjectDict):
    def __init__(self, par):
        super().__init__(par)

    def __getattr__(self, key):
        if ((key == "value" and "value" not in self.data) or
            (key == "type" and "type" not in self.data)) and \
                hasattr(self, "save_as_artifact"):
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    download_artifact(self, path=tmpdir)
                    fs = os.listdir(tmpdir)
                    assert len(fs) == 1
                    with open(os.path.join(tmpdir, fs[0]), "r") as f:
                        content = jsonpickle.loads(f.read())
                        self.value = content
                        # For backward compatibility
                        # TODO: delete me in the future
                        if isinstance(content, dict) and "value" in content:
                            if "type" in content:
                                self.type = content["type"]
                            if "type" in content and \
                                    content["type"] != str(str):
                                self.value = jsonpickle.loads(content["value"])
                            else:
                                self.value = content["value"]
                except Exception as e:
                    logger.warning("Failed to load parameter value from "
                                   "artifact: %s" % e)
        if key == "value" and hasattr(self, "description") and \
                self.description is not None:
            desc = jsonpickle.loads(self.description)
            if desc["type"] != str(str):
                try:
                    return jsonpickle.loads(super().__getattr__("value"))
                except Exception as e:
                    logger.warning("Failed to unpickle parameter: %s" % e)
        return super().__getattr__(key)


class ArgoStep(ArgoObjectDict):
    def __init__(self, step, workflow=None):
        super().__init__(deepcopy(step))
        self.workflow = workflow
        self.pod = None
        self.key = None
        if hasattr(self, "inputs"):
            self.handle_io(self.inputs)
            if hasattr(self.inputs, "parameters") and "dflow_key" in \
                    self.inputs.parameters and self.inputs.parameters[
                        "dflow_key"].value != "":
                self.key = self.inputs.parameters["dflow_key"].value

        if hasattr(self, "outputs"):
            self.handle_io(self.outputs)

    def handle_io(self, io):
        if hasattr(io, "parameters") and \
                isinstance(io.parameters, ArgoObjectList):
            io.parameters = {par.name: ArgoParameter(par)
                             for par in io.parameters}

        if hasattr(io, "artifacts") and \
                isinstance(io.artifacts, ArgoObjectList):
            io.artifacts = {art.name: art for art in io.artifacts}

        self.handle_big_parameters(io)

    def handle_big_parameters(self, io):
        if hasattr(io, "artifacts"):
            for name, art in io.artifacts.items():
                if name[:13] == "dflow_bigpar_":
                    if not hasattr(io, "parameters"):
                        io.parameters = {}
                    if name[13:] not in io.parameters:
                        par = art.copy()
                        par["name"] = name[13:]
                        par["save_as_artifact"] = True
                        io.parameters[name[13:]] = ArgoParameter(par)

    def modify_output_parameter(
            self,
            name: str,
            value: Any,
    ) -> None:
        """
        Modify output parameter of an Argo step

        Args:
            name: parameter name
            value: new value
        """
        if isinstance(value, str):
            self.outputs.parameters[name].value = value
        else:
            self.outputs.parameters[name].value = jsonpickle.dumps(value)

        if hasattr(self.outputs.parameters[name], "save_as_artifact"):
            with tempfile.TemporaryDirectory() as tmpdir:
                path = tmpdir + "/" + name
                with open(path, "w") as f:
                    f.write(jsonpickle.dumps(value))
                key = upload_s3(path)
                s3 = S3Artifact(key=key)
                if s3_config["repo_type"] == "s3":
                    self.outputs.artifacts["dflow_bigpar_" + name].s3 = \
                        ArgoObjectDict(s3.to_dict())
                elif s3_config["repo_type"] == "oss":
                    self.outputs.artifacts["dflow_bigpar_" + name].oss = \
                        ArgoObjectDict(s3.oss().to_dict())

    def modify_output_artifact(
            self,
            name: str,
            s3: S3Artifact,
    ) -> None:
        """
        Modify output artifact of an Argo step

        Args:
            name: artifact name
            s3: replace the artifact with a s3 object
        """
        if config["mode"] == "debug":
            self.outputs.artifacts[name].local_path = s3.local_path
            return
        assert isinstance(s3, S3Artifact), "must provide a S3Artifact object"
        if s3_config["repo_type"] == "s3":
            self.outputs.artifacts[name].s3 = ArgoObjectDict(s3.to_dict())
        elif s3_config["repo_type"] == "oss":
            self.outputs.artifacts[name].oss = ArgoObjectDict(
                s3.oss().to_dict())
        if s3.key[-4:] == ".tgz" and hasattr(self.outputs.artifacts[name],
                                             "archive"):
            del self.outputs.artifacts[name]["archive"]
        elif s3.key[-4:] != ".tgz" and not hasattr(self.outputs.artifacts[
                name], "archive"):
            self.outputs.artifacts[name]["archive"] = {"none": {}}

    def download_sliced_output_artifact(
            self,
            name: str,
            path: os.PathLike = ".",
    ) -> None:
        """
        Download output artifact of a sliced step

        Args:
            name: artifact name
            path: local path
        """
        assert (hasattr(self, "outputs") and
                hasattr(self.outputs, "parameters") and
                "dflow_%s_path_list" % name in self.outputs.parameters), \
            "%s is not sliced output artifact" % name
        path_list = jsonpickle.loads(
            self.outputs.parameters["dflow_%s_path_list" % name].value)
        for item in path_list:
            sub_path = item["dflow_list_item"]
            if config["mode"] == "debug":
                os.makedirs(os.path.dirname(os.path.join(path, sub_path)),
                            exist_ok=True)
                os.symlink(
                    os.path.join(self.outputs.artifacts[name].local_path,
                                 sub_path), os.path.join(path, sub_path))
            else:
                download_s3(get_key(self.outputs.artifacts[name]) + "/" +
                            sub_path, path=os.path.join(path, sub_path))

    def upload_and_modify_sliced_output_artifact(
            self,
            name: str,
            path: Union[os.PathLike, List[os.PathLike]],
    ) -> None:
        """
        Upload and modify output artifact of a sliced step

        Args:
            name: artifact name
            path: local path to be uploaded
        """
        assert (hasattr(self, "outputs") and
                hasattr(self.outputs, "parameters") and
                "dflow_%s_path_list" % name in self.outputs.parameters), \
            "%s is not sliced output artifact" % name
        path_list = jsonpickle.loads(
            self.outputs.parameters["dflow_%s_path_list" % name].value)
        if not isinstance(path, list):
            path = [path]
        assert len(path_list) == len(path), "Require %s paths, %s paths"\
            " provided" % (len(path_list), len(path))
        path_list.sort(key=lambda x: x['order'])
        new_path = [None] * (path_list[-1]['order'] + 1)
        for local_path, item in zip(path, path_list):
            new_path[item["order"]] = local_path
        s3 = upload_artifact(new_path, archive=None)
        self.modify_output_artifact(name, s3)

    def get_pod(self):
        assert self.type == "Pod"
        wf_name = self.workflow
        node_name = self.name
        template_name = self.templateName
        node_id = self.id
        pod_name = get_pod_name(wf_name, node_name, template_name, node_id)
        core_v1_api = get_k8s_core_v1_api()
        self.pod = core_v1_api.api_client.call_api(
            '/api/v1/namespaces/%s/pods/%s' % (config["namespace"], pod_name),
            'GET', response_type='V1Pod', header_params=config["http_headers"],
            _return_http_data_only=True)
        return self.pod

    def get_script(self):
        if self.pod is None:
            self.get_pod()
        main_container = next(filter(lambda c: c.name == "main",
                                     self.pod.spec.containers))
        templ_env = next(filter(lambda e: e.name == "ARGO_TEMPLATE",
                                main_container.env))
        templ = json.loads(templ_env.value)
        return templ["script"]["source"]

    def set_script(self, script):
        if self.pod is None:
            self.get_pod()
        main_container = next(filter(lambda c: c.name == "main",
                                     self.pod.spec.containers))
        templ_env = next(filter(lambda e: e.name == "ARGO_TEMPLATE",
                                main_container.env))
        templ = json.loads(templ_env.value)
        templ["script"]["source"] = script
        templ_env.value = json.dumps(templ)
        init_container = next(filter(lambda c: c.name == "init",
                                     self.pod.spec.init_containers))
        templ_env = next(filter(lambda e: e.name == "ARGO_TEMPLATE",
                                init_container.env))
        templ_env.value = json.dumps(templ)
        wait_container = next(filter(lambda c: c.name == "wait",
                                     self.pod.spec.containers))
        templ_env = next(filter(lambda e: e.name == "ARGO_TEMPLATE",
                                wait_container.env))
        templ_env.value = json.dumps(templ)

    def replay(self):
        if self.pod is None:
            self.get_pod()
        self.pod.metadata.resource_version = None
        core_v1_api = get_k8s_core_v1_api()
        try:
            core_v1_api.api_client.call_api(
                '/api/v1/namespaces/%s/pods/%s' % (config["namespace"],
                                                   self.pod.metadata.name),
                'DELETE', response_type='V1Pod',
                header_params=config["http_headers"],
                _return_http_data_only=True)
        except Exception as e:
            logging.warn("Failed to delete pod", e)
        core_v1_api.api_client.call_api(
            '/api/v1/namespaces/%s/pods' % config["namespace"],
            'POST', body=self.pod, response_type='V1Pod',
            header_params=config["http_headers"], _return_http_data_only=True)


max_k8s_resource_name_length = 253
k8s_naming_hash_length = 10


def get_pod_name(wf_name, node_name, template_name, node_id):
    if wf_name == node_name:
        return wf_name
    prefix = "%s-%s" % (wf_name, template_name)
    max_prefix_length = max_k8s_resource_name_length - k8s_naming_hash_length
    if len(prefix) > max_prefix_length - 1:
        prefix = prefix[:max_prefix_length-1]
    hash_val = fnva(node_name.encode(), FNV1_32_INIT, FNV_32_PRIME, 2**32)
    return "%s-%s" % (prefix, hash_val)


FNV_32_PRIME = 0x01000193
FNV1_32_INIT = 0x811c9dc5


def fnva(data, hval_init, fnv_prime, fnv_size):
    """
    Alternative FNV hash algorithm used in FNV-1a.
    """
    assert isinstance(data, bytes)

    hval = hval_init
    for byte in data:
        hval = hval ^ byte
        hval = (hval * fnv_prime) % fnv_size
    return hval


class ArgoWorkflow(ArgoObjectDict):
    def get_step(
            self,
            name: Union[str, List[str]] = None,
            key: Union[str, List[str]] = None,
            phase: Union[str, List[str]] = None,
            id: Union[str, List[str]] = None,
            type: Union[str, List[str]] = None,
    ) -> List[ArgoStep]:
        if name is not None and not isinstance(name, list):
            name = [name]
        if key is not None and not isinstance(key, list):
            key = [key]
        if phase is not None and not isinstance(phase, list):
            phase = [phase]
        if id is not None and not isinstance(id, list):
            id = [id]
        if type is not None and not isinstance(type, list):
            type = [type]
        step_list = []
        if hasattr(self.status, "nodes"):
            for step in self.status.nodes.values():
                if step["startedAt"] is None:
                    continue
                if name is not None and not match(step["displayName"], name):
                    continue
                if key is not None:
                    step_key = None
                    if "inputs" in step and "parameters" in step["inputs"]:
                        for par in step["inputs"]["parameters"]:
                            if par["name"] == "dflow_key":
                                step_key = par["value"]
                    if step_key not in key:
                        continue
                if phase is not None and not ("phase" in step and
                                              step["phase"] in phase):
                    continue
                if type is not None and not ("type" in step and
                                             step["type"] in type):
                    continue
                if id is not None and step["id"] not in id:
                    continue
                step = ArgoStep(step, self.metadata.name)
                step_list.append(step)
        step_list.sort(key=lambda x: x["startedAt"])
        return step_list


def match(n, names):
    for name in names:
        if n == name or n.find(name + "(") == 0:
            return True
    return False
