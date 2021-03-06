import logging
import claripy

from .plugin import SimStatePlugin
from .. import sim_options as o
from ..errors import AngrError


l = logging.getLogger("angr.state_plugins.preconstrainer")


class SimStatePreconstrainer(SimStatePlugin):
    """
    This state plugin manages the concept of preconstraining - adding constraints which you would like to remove later.

    :param constrained_addrs : Addresses which have had constraints applied to them and should not be removed.
    """

    def __init__(self, constrained_addrs=None):
        SimStatePlugin.__init__(self)

        # map of variable string names to preconstraints, for re-applying constraints.
        self.variable_map = {}
        self.preconstraints = []
        self._constrained_addrs = [] if constrained_addrs is None else constrained_addrs
        self.address_concretization = []

    def merge(self, others, merge_conditions, common_ancestor=None): # pylint: disable=unused-argument
        l.warning("Merging is not implemented for preconstrainer!")
        return False

    def widen(self, others): # pylint: disable=unused-argument
        l.warning("Widening is not implemented for preconstrainer!")
        return False

    @SimStatePlugin.memo
    def copy(self, memo): # pylint: disable=unused-argument
        c = SimStatePreconstrainer(constrained_addrs=self._constrained_addrs)

        c.variable_map = dict(self.variable_map)
        c.preconstraints = list(self.preconstraints)
        c.address_concretization = list(self.address_concretization)

        return c

    def preconstrain(self, value, variable):
        """
        Add a preconstraint that ``variable == value`` to the state.

        :param value:       The concrete value. Can be a bitvector or a bytestring or an integer.
        :param variable:    The BVS to preconstrain.
        """
        if not isinstance(value, claripy.ast.Base):
            value = self.state.solver.BVV(value, len(variable))
        elif value.op != 'BVV':
            raise ValueError("Passed a value to preconstrain that was not a BVV or a string")

        constraint = variable == value
        l.debug("Preconstraint: %s", constraint)

        # add the constraint for reconstraining later
        self.variable_map[next(iter(variable.variables))] = constraint
        self.preconstraints.append(constraint)
        if o.REPLACEMENT_SOLVER in self.state.options:
            self.state.solver._solver.add_replacement(variable, value, invalidate_cache=False)
        else:
            self.state.add_constraints(*self.preconstraints)
        if not self.state.satisfiable():
            l.warning("State went unsat while adding preconstraints")

    def preconstrain_file(self, content, simfile, set_length=False):
        """
        Preconstrain the contents of a file.

        :param content:     The content to preconstrain the file to. Can be a bytestring or a list thereof.
        :param simfile:     The actual simfile to preconstrain
        """
        repair_entry_state_opts = False
        if o.TRACK_ACTION_HISTORY in self.state.options:
            repair_entry_state_opts = True
            self.state.options -= {o.TRACK_ACTION_HISTORY}

        if set_length: # disable read bounds
            simfile.has_end = False

        pos = 0
        for write in content:
            data, length, pos = simfile.read(pos, len(write), short_reads=False)
            if not claripy.is_true(length == len(write)):
                raise AngrError("Bug in either SimFile or in usage of preconstrainer: couldn't get requested data from file")
            self.preconstrain(write, data)

        # if the file is a stream, reset its position
        if simfile.pos is not None:
            simfile.pos = 0

        if set_length: # enable read bounds; size is now maximum size
            simfile.has_end = True

        if repair_entry_state_opts:
            self.state.options |= {o.TRACK_ACTION_HISTORY}

    def preconstrain_flag_page(self, magic_content):
        """
        Preconstrain the data in the flag page.

        :param magic_content:   The content of the magic page as a bytestring.
        """
        for m, v in zip(magic_content, self.state.cgc.flag_bytes):
            self.preconstrain(m, v)

    def remove_preconstraints(self, to_composite_solver=True, simplify=True):
        if not self.preconstraints:
            return

        # cache key set creation
        precon_cache_keys = set()

        for con in self.preconstraints:
            precon_cache_keys.add(con.cache_key)

        # if we used the replacement solver we didn't add constraints we need to remove so keep all constraints
        if o.REPLACEMENT_SOLVER in self.state.options:
            new_constraints = self.state.se.constraints
        else:
            new_constraints = filter(lambda x: x.cache_key not in precon_cache_keys, self.state.se.constraints)


        if self.state.has_plugin("zen_plugin"):
            new_constraints = self.state.get_plugin("zen_plugin").filter_constraints(new_constraints)

        if to_composite_solver:
            self.state.options.discard(o.REPLACEMENT_SOLVER)
            self.state.options.add(o.COMPOSITE_SOLVER)

        self.state.release_plugin('solver')
        self.state.add_constraints(*new_constraints)

        l.debug("downsizing unpreconstrained state")
        self.state.downsize()

        if simplify:
            l.debug("simplifying solver...")
            self.state.solver.simplify()
            l.debug("...simplification done")

        self.state.solver._solver.result = None

    def reconstrain(self):
        """
        Re-apply preconstraints to improve solver time, hopefully these
        constraints still allow us to do meaningful things to state.
        """

        # test all solver splits
        subsolvers = self.state.solver._solver.split()

        for solver in subsolvers:
            solver.timeout = 1000 * 10  # 10 seconds
            if not solver.satisfiable():
                for var in solver.variables:
                    if var in self.variable_map:
                        self.state.add_constraints(self.variable_map[var])
                    else:
                        l.warning("var %s not found in self.variable_map", var)


from angr.sim_state import SimState
SimState.register_default('preconstrainer', SimStatePreconstrainer)
