import enum
import importlib.util
import inspect
import os.path
import sys
import types
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Type,
    Union,
    cast,
)
from unittest.mock import patch

try:
    from typing import get_args, get_origin
except ImportError:  # Python < 3.8
    from typing_compat import get_args, get_origin  # type: ignore

from ._vendor import yaml
from ._vendor.pydantic import BaseModel, Field, create_model
from ._vendor.pydantic.fields import FieldInfo

# Added in Python 3.9. Can be from typing if we drop support for <3.9
from ._vendor.typing_extensions import Annotated

from .errors import ConfigDoesNotExist, PredictorNotSet
from .types import (
    File as CogFile,
)
from .types import Input
from .types import (
    Path as CogPath,
)

ALLOWED_INPUT_TYPES: List[Type[Any]] = [str, int, float, bool, CogFile, CogPath]


class BasePredictor(ABC):
    def setup(
        self, weights: Optional[Union[CogFile, CogPath, str]] = None
    ) -> Optional[Awaitable[None]]:
        """
        An optional method to prepare the model so multiple predictions run efficiently.
        """
        return

    @abstractmethod
    def predict(self, **kwargs: Any) -> Any:
        """
        Run a single prediction on the model
        """
        pass

    def log(self, *messages: str) -> None:
        """
        Write a log message that will be tagged with the current prediction
        even during concurrent predictions. At runtime this method is overriden.
        """
        print(*messages)


def run_setup(predictor: BasePredictor) -> None:
    weights = get_weights_argument(predictor)
    if weights:
        predictor.setup(weights=weights)
    else:
        predictor.setup()


async def run_setup_async(predictor: BasePredictor) -> None:
    weights = get_weights_argument(predictor)
    maybe_coro = predictor.setup(weights=weights) if weights else predictor.setup()
    if maybe_coro:
        return await maybe_coro


def get_weights_argument(
    predictor: BasePredictor,
) -> Union[CogFile, CogPath, str, None]:
    # by the time we get here we assume predictor has a setup method
    weights_type = get_weights_type(predictor.setup)
    if weights_type is None:
        return None
    weights_url = os.environ.get("COG_WEIGHTS")
    weights_path = "weights"  # this is the source of a bug isn't it?

    # TODO: Cog{File,Path}.validate(...) methods accept either "real"
    # paths/files or URLs to those things. In future we can probably tidy this
    # up a little bit.
    # TODO: CogFile/CogPath should have subclasses for each of the subtypes

    # this is a breaking change
    # previously, CogPath wouldn't be converted in setup(); now it is
    # essentially everyone needs to switch from Path to str (or a new URL type)
    if weights_url:
        if weights_type == CogFile:
            return cast(CogFile, CogFile.validate(weights_url))
        if weights_type == CogPath:
            # TODO: So this can be a url. evil!
            return cast(CogPath, CogPath.validate(weights_url))
        if weights_type == str:
            return weights_url
        raise ValueError(
            f"Predictor.setup() has an argument 'weights' of type {weights_type}, but only File, Path and str are supported"
        )
    if os.path.exists(weights_path):
        if weights_type == CogFile:
            return cast(CogFile, open(weights_path, "rb"))
        if weights_type == CogPath:
            return CogPath(weights_path)
        raise ValueError(
            f"Predictor.setup() has an argument 'weights' of type {weights_type}, but only File, Path and str are supported"
        )
    return None


def get_weights_type(
    setup_function: Callable[[Any], Optional[Awaitable[None]]],
) -> Optional[Any]:
    signature = inspect.signature(setup_function)
    if "weights" not in signature.parameters:
        return None
    Type = signature.parameters["weights"].annotation
    # Handle Optional. It is Union[Type, None]
    if get_origin(Type) == Union:
        args = get_args(Type)
        if len(args) == 2 and args[1] is type(None):
            Type = get_args(Type)[0]
    return Type


def run_prediction(
    predictor: BasePredictor,
    inputs: Dict[Any, Any],
    cleanup_functions: List[Callable[[], None]],
) -> Any:
    """
    Run the predictor on the inputs, and append resulting paths
    to cleanup functions for removal.
    """
    result = predictor.predict(**inputs)
    if isinstance(result, Path):
        cleanup_functions.append(result.unlink)
    return result


# TODO: make config a TypedDict
def load_config() -> Dict[str, Any]:
    """
    Reads cog.yaml and returns it as a dict.
    """
    # Assumes the working directory is /src
    config_path = os.path.abspath("cog.yaml")
    try:
        with open(config_path) as fh:
            config = yaml.safe_load(fh)
    except FileNotFoundError as e:
        raise ConfigDoesNotExist(
            f"Could not find {config_path}",
        ) from e
    return config


def load_predictor(config: Dict[str, Any]) -> BasePredictor:
    """
    Constructs an instance of the user-defined Predictor class from a config.
    """

    ref = get_predictor_ref(config)
    return load_predictor_from_ref(ref)


def get_predictor_ref(config: Dict[str, Any], mode: str = "predict") -> str:
    if mode not in ["predict", "train"]:
        raise ValueError(f"Invalid mode: {mode}")

    if mode not in config:
        raise PredictorNotSet(
            f"Can't run predictions: '{mode}' option not found in cog.yaml"
        )

    return config[mode]


def load_predictor_from_ref(ref: str) -> BasePredictor:
    module_path, class_name = ref.split(":", 1)
    module_name = os.path.basename(module_path).split(".py", 1)[0]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None

    # Remove any sys.argv while importing predictor to avoid conflicts when
    # user code calls argparse.Parser.parse_args in production
    with patch("sys.argv", sys.argv[:1]):
        spec.loader.exec_module(module)

    predictor = getattr(module, class_name)
    # It could be a class or a function
    if inspect.isclass(predictor):
        return predictor()
    return predictor


# Base class for inputs, constructed dynamically in get_input_type().
# (This can't be a docstring or it gets passed through to the schema.)
class BaseInput(BaseModel):
    class Config:
        # When using `choices`, the type is converted into an enum to validate
        # But, after validation, we want to pass the actual value to predict(), not the enum object
        use_enum_values = True

    def cleanup(self) -> None:
        """
        Cleanup any temporary files created by the input.
        """
        for _, value in self:
            # # Handle URLPath objects specially for cleanup.
            # if isinstance(value, URLPath):
            #     value.unlink()
            # Note this is pathlib.Path, of which cog.Path is a subclass of.
            # A pathlib.Path object shouldn't make its way here,
            # but both have an unlink() method, so may as well be safe.
            #
            # URLTempFile, DataURLTempFilePath, pathlib.Path, doesn't matter
            # everyone can be unlinked
            if isinstance(value, Path):
                try:
                    value.unlink()
                except FileNotFoundError:
                    pass

    # if we had a separate method to traverse the input and apply some function to each value
    # we could have cleanup/get_tempfile/convert functions that operate on a single value
    # and do it that way. convert is supposed to mutate though, so it's tricky


def validate_input_type(type: Type[Any], name: str) -> None:
    if type is inspect.Signature.empty:
        raise TypeError(
            f"No input type provided for parameter `{name}`. Supported input types are: {readable_types_list(ALLOWED_INPUT_TYPES)}, or a Union or List of those types."
        )
    elif type not in ALLOWED_INPUT_TYPES:
        if get_origin(type) in (Union, List, list) or (
            hasattr(types, "UnionType") and get_origin(type) is types.UnionType
        ):  # noqa: E721
            for t in get_args(type):
                validate_input_type(t, name)
        else:
            raise TypeError(
                f"Unsupported input type {human_readable_type_name(type)} for parameter `{name}`. Supported input types are: {readable_types_list(ALLOWED_INPUT_TYPES)}, or a Union or List of those types."
            )


def get_input_create_model_kwargs(signature: inspect.Signature) -> Dict[str, Any]:
    create_model_kwargs = {}

    order = 0

    for name, parameter in signature.parameters.items():
        InputType = parameter.annotation

        validate_input_type(InputType, name)

        # if no default is specified, create an empty, required input
        if parameter.default is inspect.Signature.empty:
            default = Input()
        else:
            default = parameter.default
            # If user hasn't used `Input`, then wrap it in that
            if not isinstance(default, FieldInfo):
                default = Input(default=default)

        # Fields aren't ordered, so use this pattern to ensure defined order
        # https://github.com/go-openapi/spec/pull/116
        default.extra["x-order"] = order
        order += 1

        # Choices!
        if default.extra.get("choices"):
            choices = default.extra["choices"]
            # It will be passed automatically as 'enum' in the schema, so remove it as an extra field.
            del default.extra["choices"]
            if InputType == str:

                class StringEnum(str, enum.Enum):
                    pass

                InputType = StringEnum(  # type: ignore
                    name, {value: value for value in choices}
                )
            elif InputType == int:
                InputType = enum.IntEnum(name, {str(value): value for value in choices})  # type: ignore
            else:
                raise TypeError(
                    f"The input {name} uses the option choices. Choices can only be used with str or int types."
                )

        create_model_kwargs[name] = (InputType, default)

    return create_model_kwargs


def get_predict(predictor: Any) -> Callable[..., Any]:
    if hasattr(predictor, "predict"):
        return predictor.predict
    return predictor


def get_input_type(predictor: BasePredictor) -> Type[BaseInput]:
    """
    Creates a Pydantic Input model from the arguments of a Predictor's predict() method.

    class Predictor(BasePredictor):
        def predict(self, text: str):
            ...

    programmatically creates a model like this:

    class Input(BaseModel):
        text: str
    """

    predict = get_predict(predictor)
    signature = inspect.signature(predict)

    return create_model(
        "Input",
        __config__=None,
        __base__=BaseInput,
        __module__=__name__,
        __validators__=None,
        **get_input_create_model_kwargs(signature),
    )  # type: ignore


def get_output_type(predictor: BasePredictor) -> Type[BaseModel]:
    """
    Creates a Pydantic Output model from the return type annotation of a Predictor's predict() method.
    """

    predict = get_predict(predictor)
    signature = inspect.signature(predict)
    OutputType: Type[BaseModel]
    if signature.return_annotation is inspect.Signature.empty:
        raise TypeError(
            """You must set an output type. If your model can return multiple output types, you can explicitly set `Any` as the output type.

For example:

    from typing import Any

    def predict(
        self,
        image: Path = Input(description="Input image"),
    ) -> Any:
        ...
"""
        )
    else:
        OutputType = signature.return_annotation

    # The type that goes in the response is a list of the yielded type
    if get_origin(OutputType) in {Iterator, AsyncIterator}:
        # Annotated allows us to attach Field annotations to the list, which we use to mark that this is an iterator
        # https://pydantic-docs.helpmanual.io/usage/schema/#typingannotated-fields
        field = Field(**{"x-cog-array-type": "iterator"})  # type: ignore
        OutputType: Type[BaseModel] = Annotated[List[get_args(OutputType)[0]], field]  # type: ignore

    name = OutputType.__name__ if hasattr(OutputType, "__name__") else ""

    if name == "Output":
        return OutputType

    # We wrap the OutputType in an Output class to
    # ensure consistent naming of the interface in the schema.
    #
    # NOTE: If the OutputType.__name__ is "TrainingOutput" then cannot use
    # `__root__` here because this will create a reference for the Object.
    # e.g.
    #   {'title': 'Output', '$ref': '#/definitions/TrainingOutput' ... }
    #
    # And this reference may conflict with other objects at which
    # point the item will be namespaced and break our parsing. e.g.
    #   {'title': 'Output', '$ref': '#/definitions/predict_TrainingOutput' ... }
    #
    # So we work around this by inheriting from the original class rather
    # than using "__root__".
    if name == "TrainingOutput":

        class Output(OutputType):  # type: ignore
            pass

        return Output
    else:

        class Output(BaseModel):
            __root__: OutputType  # type: ignore

        return Output


def get_train(predictor: Any) -> Callable[..., Any]:
    if hasattr(predictor, "train"):
        return predictor.train
    return predictor


def get_training_input_type(predictor: BasePredictor) -> Type[BaseInput]:
    """
    Creates a Pydantic Input model from the arguments of a Predictor's train() method.

    def train(self, text: str):
        ...

    programmatically creates a model like this:

    class TrainingInput(BaseModel):
        text: str
    """

    train = get_train(predictor)
    signature = inspect.signature(train)

    return create_model(
        "TrainingInput",
        __config__=None,
        __base__=BaseInput,
        __module__=__name__,
        __validators__=None,
        **get_input_create_model_kwargs(signature),
    )  # type: ignore


def get_training_output_type(predictor: BasePredictor) -> Type[BaseModel]:
    """
    Creates a Pydantic Output model from the return type annotation of a train() method.
    """

    train = get_train(predictor)
    signature = inspect.signature(train)

    if signature.return_annotation is inspect.Signature.empty:
        raise TypeError(
            """You must set an output type. If your model can return multiple output types, you can explicitly set `Any` as the output type.

For example:

    from typing import Any

    def train(
        self,
        n: int
    ) -> Any:
        ...
"""
        )
    else:
        TrainingOutputType = signature.return_annotation

    name = (
        TrainingOutputType.__name__ if hasattr(TrainingOutputType, "__name__") else ""
    )
    # We wrap the OutputType in a TrainingOutput class to
    # ensure consistent naming of the interface in the schema
    # See comment in get_output_type for more info.
    if name == "TrainingOutput":
        return TrainingOutputType

    if name == "Output":

        class TrainingOutput(TrainingOutputType):  # type: ignore
            pass

        return TrainingOutput

    class TrainingOutput(BaseModel):
        __root__: TrainingOutputType  # type: ignore

    return TrainingOutput


def human_readable_type_name(t: Type[Any]) -> str:
    """
    Generates a useful-for-humans label for a type. For builtin types, it's just the class name (eg "str" or "int"). For other types, it includes the module (eg "pathlib.Path" or "cog.File").

    The special case for Cog modules is because the type lives in `cog.types` internally, but just `cog` when included as a dependency.
    """
    module = t.__module__
    if module == "builtins":
        return t.__qualname__
    elif module.split(".")[0] == "cog":
        module = "cog"

    try:
        return module + "." + t.__qualname__
    except AttributeError:
        return str(t)


def readable_types_list(type_list: List[Type[Any]]) -> str:
    return ", ".join(human_readable_type_name(t) for t in type_list)
