"""A frame of an abstract VM for type analysis of python bytecode."""

import logging
from typing import Any, FrozenSet, List, Mapping, Optional, Sequence, Set, Type

import immutabledict
from pycnite import marshal as pyc_marshal
from pytype.blocks import blocks
from pytype.rewrite import context
from pytype.rewrite import stack
from pytype.rewrite.abstract import abstract
from pytype.rewrite.flow import conditions
from pytype.rewrite.flow import frame_base
from pytype.rewrite.flow import variables

log = logging.getLogger(__name__)

_EMPTY_MAP = immutabledict.immutabledict()

# Type aliases
_AbstractVariable = variables.Variable[abstract.BaseValue]
_VarMap = Mapping[str, _AbstractVariable]
_FrameFunction = abstract.InterpreterFunction['Frame']


class _ShadowedNonlocals:
  """Tracks shadowed nonlocal names."""

  def __init__(self):
    self._enclosing: Set[str] = set()
    self._globals: Set[str] = set()

  def add_enclosing(self, name: str) -> None:
    self._enclosing.add(name)

  def add_global(self, name: str) -> None:
    self._globals.add(name)

  def has_enclosing(self, name: str):
    return name in self._enclosing

  def has_global(self, name: str):
    return name in self._globals

  def get_global_names(self) -> FrozenSet[str]:
    return frozenset(self._globals)

  def get_enclosing_names(self) -> FrozenSet[str]:
    return frozenset(self._enclosing)


class Frame(frame_base.FrameBase[abstract.BaseValue]):
  """Virtual machine frame.

  Attributes:
    name: The name of the frame.
    final_locals: The final `locals` dictionary after the frame finishes
      executing, with Variables flattened to BaseValues.
  """

  def __init__(
      self,
      ctx: context.Context,
      name: str,
      code: blocks.OrderedCode,
      *,
      initial_locals: _VarMap = _EMPTY_MAP,
      initial_enclosing: _VarMap = _EMPTY_MAP,
      initial_globals: _VarMap = _EMPTY_MAP,
      f_back: Optional['Frame'] = None,
  ):
    super().__init__(code, initial_locals)
    self._ctx = ctx
    self.name = name  # name of the frame
    # Final values of locals, unwrapped from variables
    self.final_locals: Mapping[str, abstract.BaseValue] = None

    # Sanity checks: a module frame should have the same locals and globals. A
    # frame should have an enclosing scope only if it has a parent (f_back).
    assert not self._is_module_frame or initial_locals == initial_globals
    assert f_back or not initial_enclosing

    # Initial variables in enclosing and global scopes
    self._initial_enclosing = initial_enclosing
    self._initial_globals = initial_globals
    self._f_back = f_back  # the frame that created this one, if any
    self._stack = stack.DataStack()  # data stack
    # Names of nonlocals shadowed in the current frame
    self._shadowed_nonlocals = _ShadowedNonlocals()
    # All functions and classes created during execution
    self._functions: List[_FrameFunction] = []
    self._classes: List[abstract.InterpreterClass] = []
    # All variables returned via RETURN_VALUE
    self._returns: List[_AbstractVariable] = []

  def __repr__(self):
    return f'Frame({self.name})'

  @classmethod
  def make_module_frame(
      cls,
      ctx: context.Context,
      code: blocks.OrderedCode,
      initial_globals: _VarMap,
  ) -> 'Frame':
    return cls(
        ctx=ctx,
        name='__main__',
        code=code,
        initial_locals=initial_globals,
        initial_enclosing={},
        initial_globals=initial_globals,
        f_back=None,
    )

  @property
  def functions(self) -> Sequence[_FrameFunction]:
    return tuple(self._functions)

  @property
  def classes(self) -> Sequence[abstract.InterpreterClass]:
    return tuple(self._classes)

  @property
  def _is_module_frame(self) -> bool:
    return self.name == '__main__'

  @property
  def stack(self) -> Sequence['Frame']:
    return (self._f_back.stack if self._f_back else []) + [self]

  def run(self) -> None:
    log.info('Running frame: %s', self.name)
    assert not self._stack
    while True:
      try:
        self.step()
      except frame_base.FrameConsumedError:
        break
    assert not self._stack
    log.info('Finished running frame: %s', self.name)
    if self._f_back and self._f_back.final_locals is None:
      live_parent = self._f_back
      log.info('Resuming frame: %s', live_parent.name)
    else:
      live_parent = None
    self._merge_nonlocals_into(live_parent)
    # Set the current state to None so that the load_* and store_* methods
    # cannot be used to modify finalized locals.
    self._current_state = None
    self.final_locals = immutabledict.immutabledict({
        name: abstract.join_values(self._ctx, var.values)
        for name, var in self._final_locals.items()})

  def store_local(self, name: str, var: _AbstractVariable) -> None:
    self._current_state.store_local(name, var)

  def store_enclosing(self, name: str, var: _AbstractVariable) -> None:
    # We shadow the name from the enclosing scope. We will merge it into f_back
    # when the current frame finishes.
    self._current_state.store_local(name, var)
    self._shadowed_nonlocals.add_enclosing(name)

  def store_global(self, name: str, var: _AbstractVariable) -> None:
    # We allow modifying globals only when executing the module frame.
    # Otherwise, we shadow the global in current frame. Either way, the behavior
    # is equivalent to storing the global as a local.
    self._current_state.store_local(name, var)
    self._shadowed_nonlocals.add_global(name)

  def store_deref(self, name: str, var: _AbstractVariable) -> None:
    # When a name from a parent frame is referenced in a child frame, we make a
    # conceptual distinction between the parent's local scope and the child's
    # enclosing scope. However, at runtime, writing to both is the same
    # operation (STORE_DEREF), so it's convenient to have a store method that
    # emulates this.
    if name in self._initial_enclosing:
      self.store_enclosing(name, var)
    else:
      self.store_local(name, var)

  def _shadows_enclosing(self, name: str) -> bool:
    """Does name shadow a variable from the enclosing scope?"""
    return self._shadowed_nonlocals.has_enclosing(name)

  def _shadows_global(self, name: str) -> bool:
    """Does name shadow a variable from the global scope?"""
    if self._is_module_frame:
      # This is the global scope, and so `name` cannot shadow anything.
      return False
    return self._shadowed_nonlocals.has_global(name)

  def load_local(self, name) -> _AbstractVariable:
    if self._shadows_enclosing(name) or self._shadows_global(name):
      raise KeyError(name)
    return self._current_state.load_local(name)

  def load_enclosing(self, name) -> _AbstractVariable:
    if self._shadows_enclosing(name):
      return self._current_state.load_local(name)
    return self._initial_enclosing[name].with_name(name)

  def load_global(self, name) -> _AbstractVariable:
    if self._shadows_global(name):
      return self._current_state.load_local(name)
    try:
      if self._is_module_frame:
        return self._current_state.load_local(name)
      else:
        return self._initial_globals[name].with_name(name)
    except KeyError:
      return self.load_builtin(name)

  def load_builtin(self, name) -> _AbstractVariable:
    builtin = self._ctx.abstract_loader.load_builtin_by_name(name)
    return builtin.to_variable(name)

  def load_deref(self, name) -> _AbstractVariable:
    # When a name from a parent frame is referenced in a child frame, we make a
    # conceptual distinction between the parent's local scope and the child's
    # enclosing scope. However, at runtime, reading from both is the same
    # operation (LOAD_DEREF), so it's convenient to have a load method that
    # emulates this.
    try:
      return self.load_local(name)
    except KeyError:
      return self.load_enclosing(name)

  def make_child_frame(
      self,
      func: _FrameFunction,
      initial_locals: Mapping[str, _AbstractVariable],
  ) -> 'Frame':
    if self._final_locals:
      current_locals = {
          name: val.to_variable() for name, val in self.final_locals.items()}
    else:
      current_locals = self._current_state.get_locals()
    initial_enclosing = {}
    for name in func.enclosing_scope:
      if name in current_locals:
        assert not self._shadows_global(name)
        initial_enclosing[name] = current_locals[name]
      else:
        initial_enclosing[name] = self._initial_enclosing[name]
    if self._is_module_frame:
      # The module frame's locals are the most up-to-date globals.
      initial_globals = current_locals
    else:
      initial_globals = dict(self._initial_globals)
      for name in self._shadowed_nonlocals.get_global_names():
        initial_globals[name] = current_locals[name]
    return Frame(
        ctx=self._ctx,
        name=func.name,
        code=func.code,
        initial_locals=initial_locals,
        initial_enclosing=initial_enclosing,
        initial_globals=initial_globals,
        f_back=self,
    )

  def get_return_value(self) -> abstract.BaseValue:
    values = sum((ret.values for ret in self._returns), ())
    return abstract.join_values(self._ctx, values)

  def _merge_nonlocals_into(self, frame: Optional['Frame']) -> None:
    # Perform any STORE_ATTR operations recorded in locals.
    for name, var in self._final_locals.items():
      target_name, dot, attr_name = name.rpartition('.')
      if not dot or target_name not in self._final_locals:
        continue
      # If the target is present on 'frame', then we merge the attribute values
      # into the frame so that any conditions on the bindings are preserved.
      # Otherwise, we store the attribute on the target.
      target_var = self._final_locals[target_name]
      if frame:
        try:
          frame_target_var = frame.load_local(target_name)
        except KeyError:
          store_on_target = True
        else:
          store_on_target = target_var.values != frame_target_var.values
      else:
        store_on_target = True
      if store_on_target:
        value = abstract.join_values(self._ctx, var.values)
        for target in target_var.values:
          log.info('Storing attribute on %r: %s -> %r',
                   target, attr_name, value)
          target.set_attribute(attr_name, value)
      else:
        frame.store_local(name, var)
    if not frame:
      return
    # Store nonlocals.
    for name in self._shadowed_nonlocals.get_enclosing_names():
      var = self._final_locals[name]
      frame.store_deref(name, var)
    for name in self._shadowed_nonlocals.get_global_names():
      var = self._final_locals[name]
      frame.store_global(name, var)

  def _call_function(
      self,
      func_var: _AbstractVariable,
      args: abstract.Args,
  ) -> None:
    ret_values = []
    for func in func_var.values:
      if isinstance(func, (abstract.SimpleFunction,
                           abstract.InterpreterClass,
                           abstract.BoundFunction)):
        ret = func.call(args)
        ret_values.append(ret.get_return_value())
      elif func is self._ctx.consts['__build_class__']:
        class_body, name = args.posargs
        builder = class_body.get_atomic_value(_FrameFunction)
        frame = builder.call(abstract.Args(frame=self))
        cls = abstract.InterpreterClass(
            ctx=self._ctx,
            name=abstract.get_atomic_constant(name, str),
            members=dict(frame.final_locals),
            functions=frame.functions,
            classes=frame.classes,
        )
        log.info('Created class: %s', cls.name)
        self._classes.append(cls)
        ret_values.append(cls)
      else:
        raise NotImplementedError('CALL not fully implemented')
    self._stack.push(
        variables.Variable(tuple(variables.Binding(v) for v in ret_values)))

  def _load_attr(
      self, target_var: _AbstractVariable, attr_name: str) -> _AbstractVariable:
    if target_var.name:
      name = f'{target_var.name}.{attr_name}'
    else:
      name = None
    try:
      # Check if we've stored the attribute in the current frame.
      return self.load_local(name)
    except KeyError as e:
      # We're loading an attribute without a locally stored value.
      attr_bindings = []
      for target in target_var.values:
        attr = target.get_attribute(attr_name)
        if not attr:
          raise NotImplementedError('Attribute error') from e
        # TODO(b/241479600): If there's a condition on the target binding, we
        # should copy it.
        attr_bindings.append(variables.Binding(attr))
      return variables.Variable(tuple(attr_bindings), name)

  def _pop_jump_if_false(self, opcode):
    unused_var = self._stack.pop()
    # TODO(b/324465215): Construct the real conditions for this jump.
    jump_state = self._current_state.with_condition(conditions.Condition())
    self._merge_state_into(jump_state, opcode.argval)
    nojump_state = self._current_state.with_condition(conditions.Condition())
    self._merge_state_into(nojump_state, opcode.next.index)

  # ---------------------------------------------------------------
  # Opcodes with no typing effects

  def byte_NOP(self, opcode):
    del opcode  # unused

  def byte_PRINT_EXPR(self, opcode):
    del opcode  # unused
    self._stack.pop_and_discard()

  def byte_PRECALL(self, opcode):
    # Internal cpython use
    del opcode  # unused

  def byte_RESUME(self, opcode):
    # Internal cpython use
    del opcode  # unused

  # ---------------------------------------------------------------
  # Load and store operations

  def byte_LOAD_CONST(self, opcode):
    constant = abstract.PythonConstant(self._ctx, self._code.consts[opcode.arg])
    self._stack.push(constant.to_variable())

  def byte_RETURN_VALUE(self, opcode):
    self._returns.append(self._stack.pop())

  def byte_STORE_NAME(self, opcode):
    self.store_local(opcode.argval, self._stack.pop())

  def byte_STORE_FAST(self, opcode):
    self.store_local(opcode.argval, self._stack.pop())

  def byte_STORE_GLOBAL(self, opcode):
    self.store_global(opcode.argval, self._stack.pop())

  def byte_STORE_DEREF(self, opcode):
    self.store_deref(opcode.argval, self._stack.pop())

  def byte_STORE_ATTR(self, opcode):
    attr_name = opcode.argval
    attr, target = self._stack.popn(2)
    if not target.name:
      raise NotImplementedError('Missing target name')
    full_name = f'{target.name}.{attr_name}'
    self.store_local(full_name, attr)

  def byte_MAKE_FUNCTION(self, opcode):
    if opcode.arg not in (0, pyc_marshal.Flags.MAKE_FUNCTION_HAS_FREE_VARS):
      raise NotImplementedError('MAKE_FUNCTION not fully implemented')
    if self._code.python_version >= (3, 11):
      code = abstract.get_atomic_constant(self._stack.pop(), blocks.OrderedCode)
      name = code.qualname
    else:
      name = abstract.get_atomic_constant(self._stack.pop(), str)
      code = abstract.get_atomic_constant(self._stack.pop(), blocks.OrderedCode)
    if opcode.arg & pyc_marshal.Flags.MAKE_FUNCTION_HAS_FREE_VARS:
      freevars = abstract.get_atomic_constant(self._stack.pop())
      enclosing_scope = tuple(freevar.name for freevar in freevars)
      assert all(enclosing_scope)
    else:
      enclosing_scope = ()
    func = abstract.InterpreterFunction(
        self._ctx, name, code, enclosing_scope, self)
    log.info('Created function: %s', func.name)
    if not (self._stack and
            self._stack.top().has_atomic_value(
                self._ctx.consts['__build_class__'])):
      # Class building makes and immediately calls a function that creates the
      # class body; we don't need to store this function for later analysis.
      self._functions.append(func)
    self._stack.push(func.to_variable())

  def byte_PUSH_NULL(self, opcode):
    del opcode  # unused
    self._stack.push(self._ctx.consts['NULL'].to_variable())

  def byte_LOAD_NAME(self, opcode):
    name = opcode.argval
    try:
      var = self.load_local(name)
    except KeyError:
      var = self.load_global(name)
    self._stack.push(var)

  def byte_LOAD_FAST(self, opcode):
    name = opcode.argval
    self._stack.push(self.load_local(name))

  def byte_LOAD_DEREF(self, opcode):
    name = opcode.argval
    self._stack.push(self.load_deref(name))

  def byte_LOAD_CLOSURE(self, opcode):
    name = opcode.argval
    self._stack.push(self.load_deref(name))

  def byte_LOAD_GLOBAL(self, opcode):
    if self._code.python_version >= (3, 11) and opcode.arg & 1:
      # Compiler-generated marker that will be consumed in byte_CALL
      # We are loading a global and calling it as a function.
      self._stack.push(self._ctx.consts['NULL'].to_variable())
    name = opcode.argval
    self._stack.push(self.load_global(name))

  def byte_LOAD_ATTR(self, opcode):
    attr_name = opcode.argval
    target_var = self._stack.pop()
    self._stack.push(self._load_attr(target_var, attr_name))

  def byte_LOAD_METHOD(self, opcode):
    method_name = opcode.argval
    instance_var = self._stack.pop()
    # https://docs.python.org/3/library/dis.html#opcode-LOAD_METHOD says that
    # this opcode should push two values onto the stack: either the unbound
    # method and its `self` or NULL and the bound method. Since we always
    # retrieve a bound method, we push the NULL
    self._stack.push(self._ctx.consts['NULL'].to_variable())
    self._stack.push(self._load_attr(instance_var, method_name))

  def byte_CALL(self, opcode):
    sentinel, *rest = self._stack.popn(opcode.arg + 2)
    if not sentinel.has_atomic_value(self._ctx.consts['NULL']):
      raise NotImplementedError('CALL not fully implemented')
    func, *args = rest
    self._call_function(func, abstract.Args(posargs=tuple(args), frame=self))

  def byte_CALL_FUNCTION(self, opcode):
    args = self._stack.popn(opcode.arg)
    func = self._stack.pop()
    self._call_function(func, abstract.Args(posargs=tuple(args), frame=self))

  def byte_CALL_METHOD(self, opcode):
    args = self._stack.popn(opcode.arg)
    func = self._stack.pop()
    # pop the NULL off the stack (see LOAD_METHOD)
    self._stack.pop_and_discard()
    self._call_function(func, abstract.Args(posargs=tuple(args), frame=self))

  # Pytype tracks variables in enclosing scopes by name rather than emulating
  # the runtime's approach with cells and freevars, so we can ignore the opcodes
  # that deal with the latter.
  def byte_MAKE_CELL(self, opcode):
    del opcode  # unused

  def byte_COPY_FREE_VARS(self, opcode):
    del opcode  # unused

  def byte_LOAD_BUILD_CLASS(self, opcode):
    self._stack.push(self._ctx.consts['__build_class__'].to_variable())

  # ---------------------------------------------------------------
  # Build and extend collections

  def _build_collection_from_stack(
      self, opcode,
      typ: Type[Any],
      factory: Type[abstract.PythonConstant] = abstract.PythonConstant
  ) -> None:
    """Pop elements off the stack and build a python constant."""
    count = opcode.arg
    elements = self._stack.popn(count)
    constant = factory(self._ctx, typ(elements))
    self._stack.push(constant.to_variable())

  def byte_BUILD_TUPLE(self, opcode):
    self._build_collection_from_stack(opcode, tuple, factory=abstract.Tuple)

  def byte_BUILD_LIST(self, opcode):
    self._build_collection_from_stack(opcode, list, factory=abstract.List)

  def byte_BUILD_SET(self, opcode):
    self._build_collection_from_stack(opcode, set, factory=abstract.Set)

  def byte_BUILD_MAP(self, opcode):
    n_elts = opcode.arg
    args = self._stack.popn(2 * n_elts)
    ret = {args[2 * i]: args[2 * i + 1] for i in range(n_elts)}
    ret = abstract.Dict(self._ctx, ret)
    self._stack.push(ret.to_variable())

  def byte_BUILD_CONST_KEY_MAP(self, opcode):
    n_elts = opcode.arg
    keys = self._stack.pop()
    # Note that `keys` is a tuple of raw python values; we do not convert them
    # to abstract objects because they are used internally to construct function
    # call args.
    keys = abstract.get_atomic_constant(keys, tuple)
    assert len(keys) == n_elts
    vals = self._stack.popn(n_elts)
    ret = dict(zip(keys, vals))
    ret = abstract.ConstKeyDict(self._ctx, ret)
    self._stack.push(ret.to_variable())

  def byte_LIST_APPEND(self, opcode):
    # Used by the compiler e.g. for [x for x in ...]
    count = opcode.arg
    val = self._stack.pop()
    # LIST_APPEND peeks back `count` elements in the stack and modifies the list
    # stored there.
    target_var = self._stack.peek(count)
    # We should only have one binding; the target is generated by the compiler.
    target = target_var.get_atomic_value()
    target.append(val)

  def byte_SET_ADD(self, opcode):
    # Used by the compiler e.g. for {x for x in ...}
    count = opcode.arg
    val = self._stack.pop()
    target_var = self._stack.peek(count)
    target = target_var.get_atomic_value()
    target.add(val)

  def byte_MAP_ADD(self, opcode):
    # Used by the compiler e.g. for {x, y for x, y in ...}
    count = opcode.arg
    # The value is at the top of the stack, followed by the key.
    key, val = self._stack.popn(2)
    target_var = self._stack.peek(count)
    target = target_var.get_atomic_value()
    target.setitem(key, val)

  # ---------------------------------------------------------------
  # Branches and jumps

  def byte_POP_JUMP_FORWARD_IF_FALSE(self, opcode):
    self._pop_jump_if_false(opcode)

  def byte_POP_JUMP_IF_FALSE(self, opcode):
    self._pop_jump_if_false(opcode)

  def byte_JUMP_FORWARD(self, opcode):
    self._merge_state_into(self._current_state, opcode.argval)

  # ---------------------------------------------------------------
  # Stack manipulation

  def byte_POP_TOP(self, opcode):
    del opcode  # unused
    self._stack.pop_and_discard()

  def byte_DUP_TOP(self, opcode):
    del opcode  # unused
    self._stack.push(self._stack.top())

  def byte_DUP_TOP_TWO(self, opcode):
    del opcode  # unused
    a, b = self._stack.popn(2)
    for v in (a, b, a, b):
      self._stack.push(v)

  def byte_ROT_TWO(self, opcode):
    del opcode  # unused
    self._stack.rotn(2)

  def byte_ROT_THREE(self, opcode):
    del opcode  # unused
    self._stack.rotn(3)

  def byte_ROT_FOUR(self, opcode):
    del opcode  # unused
    self._stack.rotn(4)

  def byte_ROT_N(self, opcode):
    self._stack.rotn(opcode.arg)
