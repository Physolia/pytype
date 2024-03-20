"""Utilities for working with abstract values."""

from typing import Any, Type, TypeVar, get_origin, overload

from pytype.rewrite.abstract import base

_T = TypeVar('_T')


@overload
def get_atomic_constant(var: base.AbstractVariableType, typ: Type[_T]) -> _T:
  ...


@overload
def get_atomic_constant(var: base.AbstractVariableType, typ: None = ...) -> Any:
  ...


def get_atomic_constant(var, typ=None):
  value = var.get_atomic_value(base.PythonConstant)
  constant = value.constant
  if typ and not isinstance(constant, (runtime_type := get_origin(typ) or typ)):
    raise ValueError(
        f'Wrong constant type for {var.display_name()}: expected '
        f'{runtime_type.__name__}, got {constant.__class__.__name__}')
  return constant


def flatten_variable(var: base.AbstractVariableType) -> base.BaseValue:
  values = var.values
  if len(values) > 1:
    return base.Union(values)
  elif values:
    return values[0]
  else:
    return base.ANY
