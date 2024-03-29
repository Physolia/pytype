"""Global VM context.

Add global state that should be shared by all frames, abstract values, etc. to
the Context object. Make sure to add only things that are truly global! The
context introduces circular dependencies that degrade the quality of typing,
cross references, and other tooling.

New Context attributes should also be added to the ContextType protocol in
abstract/base.py.
"""

from pytype.errors import errors
from pytype.rewrite import convert
from pytype.rewrite import output
from pytype.rewrite import pretty_printer
from pytype.rewrite.abstract import abstract


class Context:
  """Global VM context."""

  # TODO(b/241479600): We have to duplicate the instance attributes here to work
  # around a weird bug in current pytype. Once rewrite/ is rolled out, this bug
  # will hopefully be gone and we can delete these duplicate declarations.
  singles: abstract.Singletons
  errorlog: errors.VmErrorLog
  abstract_converter: convert.AbstractConverter
  pytd_converter: output.PytdConverter

  def __init__(self):
    self.singles = abstract.Singletons(self)
    self.errorlog = errors.VmErrorLog(pretty_printer.PrettyPrinter(self))
    self.abstract_converter = convert.AbstractConverter(self)
    self.pytd_converter = output.PytdConverter(self)
