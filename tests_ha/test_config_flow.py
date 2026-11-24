"""Fresh-install config flow tests (issue #23).

uv run --python 3.13 --with homeassistant==2026.1.2 python -m unittest discover -s tests_ha
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.melodious.config_flow import MelodiousConfigFlow
from custom_components.melodious.const import CONF_ENTITIES, CONF_WEBHOOK_ID, DOMAIN
from custom_components.melodious.hub import HubError, HubRefused

USER_INPUT = {
    "host": "192.168.1.50",
    "port": 8080,
    "username": "",
    "password": "",
    "ha_url": "http://192.168.1.10:8123",
}


def make_flow(commit_side_effect=None, commit_result=None):
    flow = MelodiousConfigFlow()
    flow.hass = SimpleNamespace()
    flow.flow_id = "fresh-install"
    flow.handler = DOMAIN
    flow.context = {"source": "user"}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    client = SimpleNamespace(
        commit=AsyncMock(return_value=commit_result or {}, side_effect=commit_side_effect)
    )
    created = {}

    def create_entry(**kwargs):
        created.update(kwargs)
        return {"type": FlowResultType.CREATE_ENTRY, **kwargs}

    flow.async_create_entry = Mock(side_effect=create_entry)
    return flow, client, created


class FreshInstallFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_render_shows_the_form(self):
        flow, _client, _created = make_flow()
        with patch("custom_components.melodious.config_flow.get_url", return_value="http://ha:8123"):
            result = await flow.async_step_user()
        self.assertEqual(result["type"], FlowResultType.FORM)
        self.assertEqual(result["step_id"], "user")

    async def test_fresh_install_creates_the_entry(self):
        flow, client, created = make_flow()
        with patch("custom_components.melodious.config_flow.hub_client", return_value=client):
            result = await flow.async_step_user(dict(USER_INPUT))
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        flow.async_set_unique_id.assert_awaited_once_with("192.168.1.50")
        client.commit.assert_awaited_once()
        self.assertEqual(created["title"], "Melodious hub 192.168.1.50")
        self.assertTrue(created["data"][CONF_WEBHOOK_ID])
        self.assertEqual(created["options"], {CONF_ENTITIES: []})

    async def test_a_refused_dry_run_still_creates_the_entry(self):
        flow, client, _created = make_flow(commit_side_effect=HubRefused("device in use"))
        with patch("custom_components.melodious.config_flow.hub_client", return_value=client):
            result = await flow.async_step_user(dict(USER_INPUT))
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)

    async def test_a_hub_error_comes_back_as_a_form_error(self):
        flow, client, _created = make_flow(commit_side_effect=HubError("no route"))
        with patch("custom_components.melodious.config_flow.hub_client", return_value=client), \
             patch("custom_components.melodious.config_flow.get_url", return_value=""):
            result = await flow.async_step_user(dict(USER_INPUT))
        self.assertEqual(result["type"], FlowResultType.FORM)
        self.assertEqual(result["errors"], {"base": "cannot_connect"})

    async def test_an_unexpected_dry_run_failure_is_a_form_error_not_a_crash(self):
        # Anything odd on first contact with the hub (a client construction
        # error, an odd answer) must surface as "Could not use the hub: ..."
        # in the form, never as Home Assistant's "Unknown error occurred".
        flow, client, _created = make_flow(commit_side_effect=RuntimeError("boom"))
        with patch("custom_components.melodious.config_flow.hub_client", return_value=client), \
             patch("custom_components.melodious.config_flow.get_url", return_value=""):
            result = await flow.async_step_user(dict(USER_INPUT))
        self.assertEqual(result["type"], FlowResultType.FORM)
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertIn("boom", result["description_placeholders"]["error"])

    async def test_an_unexpected_client_failure_is_a_form_error_not_a_crash(self):
        flow, _client, _created = make_flow()
        with patch(
            "custom_components.melodious.config_flow.hub_client",
            side_effect=ValueError("bad credentials encoding"),
        ), patch("custom_components.melodious.config_flow.get_url", return_value=""):
            result = await flow.async_step_user(dict(USER_INPUT))
        self.assertEqual(result["type"], FlowResultType.FORM)
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertIn("bad credentials encoding", result["description_placeholders"]["error"])


if __name__ == "__main__":
    unittest.main()
