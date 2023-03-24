"""Implementation of types from the fiddle library."""

from typing import Any, Dict, Tuple

from pytype.abstract import abstract
from pytype.abstract import function
from pytype.abstract import mixin
from pytype.overlays import classgen
from pytype.overlays import overlay
from pytype.pytd import pytd


# Type aliases so we aren't importing stuff purely for annotations
Node = Any
Variable = Any


# Cache instances, so that we don't generate two different classes when
# Config[Foo] is used in two separate places. We use a tuple of the abstract
# class of Foo and a string (either "Config" or "Partial") as a key and store
# the generated Buildable instance (either Config or Partial) as a value.
_INSTANCE_CACHE: Dict[Tuple[abstract.Class, str], abstract.Instance] = {}


class FiddleOverlay(overlay.Overlay):
  """A custom overlay for the 'fiddle' module."""

  def __init__(self, ctx):
    """Initializes the FiddleOverlay.

    This function loads the AST for the fiddle module, which is used to
    access type information for any members that are not explicitly provided by
    the overlay. See get_attribute in attribute.py for how it's used.

    Args:
      ctx: An instance of context.Context.
    """
    member_map = {
        "Config": ConfigBuilder,
        "Partial": PartialBuilder,
    }
    ast = ctx.loader.import_name("fiddle")
    super().__init__(ctx, "fiddle", member_map, ast)


class BuildableBuilder(abstract.PyTDClass, mixin.HasSlots):
  """Factory for creating fiddle.Config classes."""

  _NAME_INDEX = 0
  BUILDABLE_NAME = ""

  def __init__(self, ctx):
    assert self.BUILDABLE_NAME, "Only instantiate BuildableBuilder subclasses."
    fiddle_ast = ctx.loader.import_name("fiddle")
    pytd_cls = fiddle_ast.Lookup(f"fiddle.{self.BUILDABLE_NAME}")
    # fiddle.Config/Partial loads as a LateType, convert to pytd.Class
    if isinstance(pytd_cls, pytd.Constant):
      pytd_cls = ctx.convert.constant_to_value(pytd_cls).pytd_cls
    super().__init__(self.BUILDABLE_NAME, pytd_cls, ctx)
    mixin.HasSlots.init_mixin(self)
    self.set_native_slot("__getitem__", self.getitem_slot)

  def __repr__(self):
    return f"Fiddle{self.BUILDABLE_NAME}"

  @classmethod
  def generate_name(cls):
    cls._NAME_INDEX += 1
    return f"{cls.BUILDABLE_NAME}_{cls._NAME_INDEX}"

  def _match_pytd_init(self, node, init_var, args):
    init = init_var.data[0]
    try:
      init.match_args(node, args)
    except function.FailedFunctionCall as e:
      self.ctx.errorlog.invalid_function_call(self.ctx.vm.frames, e)

  def _match_interpreter_init(self, node, init_var, args):
    # Buildables support partial initialization, so give every parameter a
    # default when matching __init__.
    init = init_var.data[0]
    for k in init.signature.param_names:
      init.signature.defaults[k] = self.ctx.new_unsolvable(node)
    # TODO(mdemello): We are calling the function and discarding the return
    # value, when ideally we should just call function.match_all_args().
    function.call_function(self.ctx, node, init_var, args)

  def new_slot(
      self, node, unused_cls, *args, **kwargs
  ) -> Tuple[Node, abstract.Instance]:
    template = args[0].data[0]
    # Configs can be initialized either with no args, e.g. Config(Class) or with
    # initial values, e.g. Config(Class, x=10, y=20). We need to check here that
    # the extra args match the underlying __init__ signature.
    if len(args) > 1 or kwargs:
      _, init_var = self.ctx.attribute_handler.get_attribute(
          node, template, "__init__")
      if _is_dataclass(template):
        # Only do init matching for dataclasses for now
        args = function.Args(posargs=args, namedargs=kwargs)
        init = init_var.data[0]
        if isinstance(init, abstract.PyTDFunction):
          self._match_pytd_init(node, init_var, args)
        else:
          self._match_interpreter_init(node, init_var, args)

    # Now create the Config object.
    node, ret = make_buildable(self.BUILDABLE_NAME, template, node, self.ctx)
    return node, ret.instantiate(node)

  def getitem_slot(self, node, index_var) -> Tuple[Node, abstract.Instance]:
    template = index_var.data[0]
    node, ret = make_buildable(self.BUILDABLE_NAME, template, node, self.ctx)
    return node, ret.to_variable(node)

  def get_own_new(self, node, value) -> Tuple[Node, Variable]:
    new = abstract.NativeFunction("__new__", self.new_slot, self.ctx)
    return node, new.to_variable(node)


class ConfigBuilder(BuildableBuilder):
  BUILDABLE_NAME = "Config"


class PartialBuilder(BuildableBuilder):
  BUILDABLE_NAME = "Partial"


class Buildable(abstract.InterpreterClass):
  def __init__(self, fiddle_type_name, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.fiddle_type_name = fiddle_type_name
    self.underlying = None


class Config(Buildable):
  """An instantiation of a fiddle.Config with a particular template."""

  def __init__(self, *args, **kwargs):
    super().__init__("Config", *args, **kwargs)


class Partial(Buildable):
  """An instantiation of a fiddle.Partial with a particular template."""

  def __init__(self, *args, **kwargs):
    super().__init__("Partial", *args, **kwargs)


def _convert_type(typ, node, ctx):
  """Helper function for recursive type conversion of fields."""

  if _is_dataclass(typ):
    _, new_typ = make_buildable("Config", typ, node, ctx)
    return abstract.Union([new_typ, typ], ctx)
  else:
    return typ


def make_buildable(
    subclass_name: str, template: abstract.Class, node, ctx
) -> Tuple[Node, abstract.BaseValue]:
  """Generate a Config from a template class."""
  if subclass_name not in ("Config", "Partial"):
    raise ValueError("Unexpected subclass_name: " + subclass_name)

  if (template, subclass_name) in _INSTANCE_CACHE:
    return node, _INSTANCE_CACHE[(template, subclass_name)]

  if _is_dataclass(template):
    fields = [classgen.Field(x.name, _convert_type(x.typ, node, ctx), x.default)
              for x in template.metadata["__dataclass_fields__"]]
    if subclass_name == "Config":
      name = ConfigBuilder.generate_name()
      interpreter_class = Config
    else:
      name = PartialBuilder.generate_name()
      interpreter_class = Partial
    props = classgen.ClassProperties(
        name=name,
        fields=fields,
        bases=[]
    )
    node, cls_var = classgen.make_interpreter_class(
        interpreter_class, props, node, ctx)
    cls = cls_var.data[0]
    cls.underlying = template
    _INSTANCE_CACHE[(template, subclass_name)] = cls
    return node, cls
  else:
    return node, ctx.convert.unsolvable


def _is_dataclass(typ) -> bool:
  return (isinstance(typ, abstract.Class) and
          "__dataclass_fields__" in typ.metadata)


def is_fiddle_buildable_pytd(cls: pytd.Class) -> bool:
  # We need the awkward check for the full name because while fiddle reexports
  # the class as fiddle.Config, we expand that in inferred pyi files to
  # fiddle._src.config.Config
  return cls.name.startswith("fiddle.") and (
      cls.name.endswith(".Config") or cls.name.endswith(".Partial"))


def get_fiddle_buildable_subclass(cls: pytd.Class) -> str:
  if cls.name.endswith(".Config"):
    return "Config"
  if cls.name.endswith(".Partial"):
    return "Partial"
  raise ValueError(f"Unexpected {cls.name} when computing fiddle Buildable "
                   "subclass; allowed suffixes are `.Config`, and `.Partial`.")
