"""Host power grants — the three legs, and the ways each one can be dodged.

Every test here is about a way an app could END UP with elevated access it was
not supposed to have, or (just as bad) end up WITHOUT access it declared while
still starting successfully. The second failure mode is the one that motivated
the feature: a Windows guest with no /dev/kvm boots into software emulation and
reads as "the app is slow and broken".
"""
import pytest

from src.apps import hostpower
from src.apps.hostpower import HostPowerError


class TestExpand:
    def test_all_expands_to_every_granular_grant(self):
        assert hostpower.expand(["all"]) == hostpower.GRANULAR

    def test_all_does_not_include_privileged(self):
        """"Every device my host can offer" and "dissolve the container
        boundary" are different decisions. A convenience keyword must not make
        the second one on the user's behalf."""
        assert "privileged" not in hostpower.expand(["all"])
        assert "privileged" in hostpower.GRANTS

    def test_all_composes_with_privileged_when_both_named(self):
        assert hostpower.expand(["all", "privileged"]) == \
            hostpower.GRANULAR + ("privileged",)

    def test_order_is_catalog_order_not_caller_order(self):
        """The set is rendered into a status line, a log line and a console
        badge. One that reorders itself between runs looks like a change."""
        assert hostpower.expand(["tun", "kvm"]) == hostpower.expand(["kvm", "tun"])

    def test_duplicates_collapse(self):
        assert hostpower.expand(["kvm", "kvm", "all"]) == hostpower.GRANULAR

    def test_unknown_grant_raises_rather_than_being_dropped(self):
        with pytest.raises(HostPowerError, match="unknown host power grant"):
            hostpower.expand(["kvm", "gpu"])

    def test_empty_and_none_are_empty(self):
        assert hostpower.expand(None) == ()
        assert hostpower.expand([]) == ()
        assert hostpower.expand(["", "  "]) == ()

    def test_parse_list_handles_the_wire_format(self):
        assert hostpower.parse_list("kvm, tun") == ("kvm", "tun")
        assert hostpower.parse_list("all") == hostpower.GRANULAR
        assert hostpower.parse_list("") == ()
        assert hostpower.parse_list(None) == ()


class TestHostGrants:
    def test_absent_env_grants_nothing(self):
        assert hostpower.host_grants({}) == ()

    def test_reads_the_env_var(self):
        assert hostpower.host_grants({hostpower.ENV_VAR: "kvm,tun"}) == ("kvm", "tun")

    def test_malformed_env_grants_nothing_instead_of_raising(self):
        """Read on every Tier-2 app load. A typo in one host's service
        definition must not make every app on that host unloadable."""
        assert hostpower.host_grants({hostpower.ENV_VAR: "kvm,nonsense"}) == ()


class TestResolveTheThreeLegs:
    ENV = {hostpower.ENV_VAR: "kvm,tun"}
    PERMS = ["host:device-kvm", "host:device-tun"]

    def test_all_three_legs_present_grants(self):
        assert hostpower.resolve("app", ["kvm"], self.PERMS, self.ENV) == ("kvm",)

    def test_no_request_needs_nothing(self):
        """An app that asks for nothing must not require a permission or a
        host opt-in — that is every app that exists today."""
        assert hostpower.resolve("app", [], [], {}) == ()
        assert hostpower.resolve("app", None, None, None) == ()

    def test_missing_capability_is_refused(self):
        with pytest.raises(HostPowerError, match="missing the matching permission"):
            hostpower.resolve("app", ["kvm"], [], self.ENV)

    def test_declaring_the_capability_without_the_host_is_refused(self):
        with pytest.raises(HostPowerError, match="has not granted"):
            hostpower.resolve("app", ["kvm"], self.PERMS, {})

    def test_host_offering_less_than_asked_is_refused(self):
        """`--host-power=all` on a host with no /dev/kvm yields an effective
        set without kvm. Asking for kvm there must fail, not half-succeed."""
        with pytest.raises(HostPowerError, match=r"needs host power \[kvm\]"):
            hostpower.resolve("app", ["kvm"], self.PERMS,
                              {hostpower.ENV_VAR: "tun,fuse"})

    def test_refusal_names_the_command_that_fixes_it(self):
        with pytest.raises(HostPowerError) as exc:
            hostpower.resolve("aw-app-windows", ["kvm", "tun"], self.PERMS, {})
        message = str(exc.value)
        assert "--host-power=kvm,tun" in message
        assert "aw-app-windows" in message

    def test_privileged_needs_its_own_capability_not_a_device_one(self):
        with pytest.raises(HostPowerError, match="host:privileged"):
            hostpower.resolve("app", ["privileged"], self.PERMS,
                              {hostpower.ENV_VAR: "privileged"})


class TestDockerKwargs:
    def test_nothing_granted_adds_nothing(self):
        """The default path for every app installed today — it must stay
        byte-identical to what the supervisor did before this existed."""
        assert hostpower.docker_kwargs(()) == {}

    def test_kvm_passes_the_device_through(self):
        assert hostpower.docker_kwargs(["kvm"]) == {"devices": ["/dev/kvm:/dev/kvm:rwm"]}

    def test_tun_also_adds_the_capability_the_device_is_useless_without(self):
        """Opening /dev/net/tun without NET_ADMIN succeeds and then fails to
        configure the interface — a guest with a NIC that carries no traffic."""
        kwargs = hostpower.docker_kwargs(["tun"])
        assert kwargs["devices"] == ["/dev/net/tun:/dev/net/tun:rwm"]
        assert kwargs["cap_add"] == ["NET_ADMIN"]

    def test_privileged_short_circuits_the_device_list(self):
        """--privileged already implies every device and capability; also
        listing them makes `inspect` read as a tighter grant than reality."""
        assert hostpower.docker_kwargs(["all", "privileged"]) == {"privileged": True}

    def test_binder_passes_every_device_it_needs(self):
        devices = hostpower.docker_kwargs(["binder"])["devices"]
        assert len(devices) == 3
        assert all(d.endswith(":rwm") for d in devices)

    def test_grants_merge_without_duplicating_capabilities(self):
        kwargs = hostpower.docker_kwargs(["tun", "fuse"])
        assert sorted(kwargs["cap_add"]) == ["NET_ADMIN", "SYS_ADMIN"]
        assert len(kwargs["devices"]) == 2


class TestDescribe:
    def test_no_grant_says_so_plainly(self):
        assert "standard" in hostpower.describe(())

    def test_privileged_is_shouted(self):
        assert "PRIVILEGED" in hostpower.describe(["privileged"])
        assert "no container isolation" in hostpower.describe(["privileged"])

    def test_granular_lists_the_grants(self):
        assert hostpower.describe(["kvm", "tun"]) == "kvm, tun"


class TestCatalogIntegrity:
    def test_every_grant_maps_to_a_real_capability(self):
        from src.apps.capabilities import CATALOG
        for name, spec in hostpower.GRANTS.items():
            assert spec["capability"] in CATALOG, name

    def test_every_host_capability_is_high_risk(self):
        """These hand out kernel-level access. A low-risk tier would let a
        side-loaded, unsigned app request them."""
        from src.apps.capabilities import is_high_risk
        for cap in hostpower.CAPABILITIES:
            assert is_high_risk(cap), cap

    def test_granular_is_a_subset_of_the_catalog(self):
        assert set(hostpower.GRANULAR) <= set(hostpower.GRANTS)
