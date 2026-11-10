"""Check modules; importing this package registers every check."""

from scmgen.validation.checks import (accounting, inventory, master_data, payables,  # noqa: F401
                                    procurement, receivables, sales, system, tax)
