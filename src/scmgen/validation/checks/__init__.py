"""Check modules; importing this package registers every check."""

from scmgen.validation.checks import (accounting, expenses, fulfilment, inventory, master_data, payables,  # noqa: F401
                                    procurement, receivables, returns, sales, system, tax)
