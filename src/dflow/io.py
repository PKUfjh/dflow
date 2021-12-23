from copy import deepcopy
from collections import UserDict
import jsonpickle
from argo.workflows.client.configuration import Configuration
from argo.workflows.client import (
    V1alpha1Inputs,
    V1alpha1Outputs,
    V1alpha1Parameter,
    V1alpha1ValueFrom,
    V1alpha1Artifact,
    V1alpha1RawArtifact,
    V1alpha1S3Artifact,
    V1alpha1ArchiveStrategy
)

class AutonamedDict(UserDict):
    def __setitem__(self, key, value):
        value = deepcopy(value)
        value.name = key
        super().__setitem__(key, value)

class Inputs:
    def __init__(self, parameters=None, artifacts=None):
        if parameters is not None:
            self.parameters = parameters
        else:
            self.parameters = AutonamedDict()
        
        if artifacts is not None:
            self.artifacts = artifacts
        else:
            self.artifacts = AutonamedDict()
    
    def __setattr__(self, key, value):
        if key in ["parameters", "artifacts"] and isinstance(value, dict):
            super().__setattr__(key, AutonamedDict(value))
        else:
            super().__setattr__(key, value)
    
    def set_step_id(self, step_id):
        for par in self.parameters.values():
            par.step_id = step_id
        for art in self.artifacts.values():
            art.step_id = step_id
    
    def convert_to_argo(self):
        return V1alpha1Inputs(parameters=[par.convert_to_argo() for par in self.parameters.values()],
                artifacts=[art.convert_to_argo() for art in self.artifacts.values()])

class Outputs:
    def __init__(self, parameters=None, artifacts=None):
        if parameters is not None:
            self.parameters = parameters
        else:
            self.parameters = AutonamedDict()
        
        if artifacts is not None:
            self.artifacts = artifacts
        else:
            self.artifacts = AutonamedDict()
    
    def __setattr__(self, key, value):
        if key in ["parameters", "artifacts"] and isinstance(value, dict):
            super().__setattr__(key, AutonamedDict(value))
        else:
            super().__setattr__(key, value)
    
    def set_step_id(self, step_id):
        for par in self.parameters.values():
            par.step_id = step_id
        for art in self.artifacts.values():
            art.step_id = step_id

    def convert_to_argo(self):
        return V1alpha1Outputs(parameters=[par.convert_to_argo() for par in self.parameters.values()],
                artifacts=[art.convert_to_argo() for art in self.artifacts.values()])

class InputParameter:
    def __init__(self, name=None, step_id=None, type=None, value=None):
        self.name = name
        self.step_id = step_id
        self.type = type
        self.value = value
    
    def __repr__(self):
        if self.name is not None:
            if self.step_id is not None:
                return "%s.inputs.parameters.%s" % (self.step_id, self.name)
            return "inputs.parameters.%s" % self.name
        return ""
    
    def convert_to_argo(self):
        if self.value is None:
            return V1alpha1Parameter(name=self.name)
        elif isinstance(self.value, InputParameter) or isinstance(self.value, OutputParameter):
            return V1alpha1Parameter(name=self.name, value="{{%s}}" % self.value)
        elif isinstance(self.value, str):
            return V1alpha1Parameter(name=self.name, value=self.value)
        else:
            return V1alpha1Parameter(name=self.name, value=jsonpickle.dumps(self.value))

class InputArtifact:
    def __init__(self, path, name=None, step_id=None, type=None, source=None):
        self.path = path
        self.name = name
        self.step_id = step_id
        self.type = type
        self.source = source
        self._sub_path = None
    
    def __repr__(self):
        if self.name is not None:
            if self.step_id is not None:
                return "%s.inputs.artifacts.%s" % (self.step_id, self.name)
            return "inputs.artifacts.%s" % self.name
        return ""

    def sub_path(self, path):
        artifact = deepcopy(self)
        artifact._sub_path = path
        return artifact

    def convert_to_argo(self):
        if self.source is None:
            return V1alpha1Artifact(name=self.name, path=self.path)
        if isinstance(self.source, InputArtifact) or isinstance(self.source, OutputArtifact):
            return V1alpha1Artifact(name=self.name, path=self.path, _from="{{%s}}" % self.source, sub_path=self.source._sub_path)
        elif isinstance(self.source, S3Artifact):
            return V1alpha1Artifact(name=self.name, path=self.path, s3=self.source, sub_path=self.source._sub_path)
        elif isinstance(self.source, str):
            return V1alpha1Artifact(name=self.name, path=self.path, raw=V1alpha1RawArtifact(data=self.source))
        else:
            raise RuntimeError("Cannot handle here")

class OutputParameter:
    def __init__(self, value_from_path, name=None, step_id=None, type=None):
        self.value_from_path = value_from_path
        self.name = name
        self.step_id = step_id
        self.type = type
    
    def __repr__(self):
        if self.name is not None:
            if self.step_id is not None:
                return "%s.outputs.parameters.%s" % (self.step_id, self.name)
            return "outputs.parameters.%s" % self.name
        return ""
    
    def convert_to_argo(self):
        return V1alpha1Parameter(name=self.name, value_from=V1alpha1ValueFrom(path=self.value_from_path))

class OutputArtifact:
    def __init__(self, path, name=None, step_id=None, type=None, save=None, archive="tar"):
        self.path = path
        self.name = name
        self.step_id = step_id
        self.type = type
        if save is None:
            save = []
        elif not isinstance(save, list):
            save = [save]
        self.save = save
        self.archive = archive
        self._sub_path = None

    def sub_path(self, path):
        artifact = deepcopy(self)
        artifact._sub_path = path
        return artifact

    def __repr__(self):
        if self.name is not None:
            if self.step_id is not None:
                return "%s.outputs.artifacts.%s" % (self.step_id, self.name)
            return "outputs.artifacts.%s" % self.name
        return ""
    
    def pvc(self):
        pvc = PVC("public", self.step_id, self.name)
        self.save.append(pvc)
        return pvc
    
    def convert_to_argo(self):
        if self.archive is None:
            archive = V1alpha1ArchiveStrategy(_none={})
        elif self.archive == "tar":
            archive = None
        else:
            raise RuntimeError("Archive type %s not supported" % self.archive)

        s3 = None
        for save in self.save:
            if isinstance(save, S3Artifact):
                s3 = deepcopy(save)
                if s3._sub_path is not None:
                    if s3.key[-1] != "/": s3.key += "/"
                    s3.key += s3._sub_path

        return V1alpha1Artifact(name=self.name, path=self.path, archive=archive, s3=s3)

class PVC:
    def __init__(self, pvcname, relpath, name):
        self.pvcname = pvcname
        self.relpath = relpath
        self.name = name

class S3Artifact(V1alpha1S3Artifact):
    def __init__(self, *args, **kwargs):
        config = Configuration()
        config.client_side_validation = False
        super().__init__(local_vars_configuration=config, *args, **kwargs)
        self._sub_path = None

    def sub_path(self, path):
        artifact = deepcopy(self)
        artifact._sub_path = path
        return artifact
