import os

import pytest


@pytest.fixture(scope="module")
def cfg():
    from server import config

    return config


TOOL_NAMES = {
    "web_search",
    "fetch_page",
    "generate_image",
    "edit_image",
    "get_user_location",
    "read_file",
    "update_user_context",
    "manage_tasks",
}


class TestEndpoints:
    def test_llama_url(self, cfg):
        assert cfg.LLAMA_URL == f"{cfg.LLAMA_BASE}/v1/chat/completions"
        assert cfg.LLAMA_BASE.startswith("http://")

    def test_searxng_url(self, cfg):
        assert cfg.SEARXNG_URL.startswith("http://")
        assert "/search" in cfg.SEARXNG_URL

    def test_comfyui_url(self, cfg):
        assert cfg.COMFYUI_URL == "http://localhost:8188"


class TestModelId:
    def test_model_id_is_nonempty_string(self, cfg):
        assert isinstance(cfg.MODEL_ID, str)
        assert cfg.MODEL_ID.strip()

    def test_cpu_model_id_is_nonempty_string(self, cfg):
        assert isinstance(cfg.MODEL_ID_CPU, str)
        assert cfg.MODEL_ID_CPU.strip()


class TestLoadModelIds:
    def test_json_gpu_and_cpu(self, cfg, tmp_path):
        f = tmp_path / "model.json"
        f.write_text('{"gpu": "g1", "cpu": "c1"}')
        assert cfg._load_model_ids(str(f), str(tmp_path / "model.txt")) == ("g1", "c1")

    def test_json_missing_cpu_falls_back_to_gpu(self, cfg, tmp_path):
        f = tmp_path / "model.json"
        f.write_text('{"gpu": "g1"}')
        assert cfg._load_model_ids(str(f), str(tmp_path / "model.txt")) == ("g1", "g1")

    def test_json_default_key(self, cfg, tmp_path):
        f = tmp_path / "model.json"
        f.write_text('{"default": "d1"}')
        assert cfg._load_model_ids(str(f), str(tmp_path / "model.txt")) == ("d1", "d1")

    def test_invalid_json_falls_back_to_model_txt(self, cfg, tmp_path):
        f = tmp_path / "model.json"
        f.write_text("{ not json")
        legacy = tmp_path / "model.txt"
        legacy.write_text("legacy\n")
        assert cfg._load_model_ids(str(f), str(legacy)) == ("legacy", "legacy")

    def test_missing_json_falls_back_to_model_txt(self, cfg, tmp_path):
        legacy = tmp_path / "model.txt"
        legacy.write_text("legacy\n")
        assert cfg._load_model_ids(str(tmp_path / "model.json"), str(legacy)) == (
            "legacy",
            "legacy",
        )

    def test_missing_both_returns_empty(self, cfg, tmp_path):
        assert cfg._load_model_ids(str(tmp_path / "model.json"), str(tmp_path / "model.txt")) == ("", "")


class TestTools:
    def test_all_expected_tools_present(self, cfg):
        names = {t["function"]["name"] for t in cfg.TOOLS}
        assert names == TOOL_NAMES

    def test_tool_structure(self, cfg):
        for tool in cfg.TOOLS:
            assert tool["type"] == "function"
            fn = tool["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn

    def test_generate_image_has_model_enum(self, cfg):
        gen = next(
            t for t in cfg.TOOLS if t["function"]["name"] == "generate_image"
        )
        props = gen["function"]["parameters"]["properties"]
        assert "model" in props
        assert set(props["model"]["enum"]) == set(cfg.IMAGE_MODELS.keys())
        assert "prompt" in props
        assert props["prompt"]["type"] == "string"

    def test_manage_tasks_operations(self, cfg):
        mt = next(
            t for t in cfg.TOOLS if t["function"]["name"] == "manage_tasks"
        )
        op = mt["function"]["parameters"]["properties"]["operation"]
        assert set(op["enum"]) == {"create", "update", "complete", "delete", "list", "get"}

    def test_web_search_required_query(self, cfg):
        ws = next(t for t in cfg.TOOLS if t["function"]["name"] == "web_search")
        assert ws["function"]["parameters"]["required"] == ["query"]

    def test_tools_token_cost_positive(self, cfg):
        assert cfg.TOOLS_TOKEN_COST > 0


class TestBuildSysContent:
    def test_placeholders_replaced(self, cfg):
        content = cfg.build_sys_content()
        assert "%model_list%" not in content
        assert "%_image_keys%" not in content
        model_list = "; ".join(
            f"{k}: {v['description']}" for k, v in cfg.IMAGE_MODELS.items()
        )
        assert model_list in content
        assert str(list(cfg.IMAGE_MODELS.keys())) in content

    def test_build_sys_content_reads_prompt_file(self, cfg):
        content = cfg.build_sys_content()
        assert len(content) > 100
        assert "Assistant" in content or "You are" in content


class TestConstants:
    def test_llama_server_args(self, cfg):
        args = cfg.LLAMA_SERVER_ARGS
        assert "--host" in args
        assert "--port" in args
        assert str(cfg.PORT) in args or "8081" in args
        assert "--n-gpu-layers" in args
        assert "--no-mmproj-offload" in args

    def test_llama_server_args_cpu(self, cfg):
        cpu_args = cfg.LLAMA_SERVER_ARGS_CPU
        assert "--host" in cpu_args
        assert "--port" in cpu_args
        assert "--n-gpu-layers" in cpu_args
        ngl = cpu_args[cpu_args.index("--n-gpu-layers") + 1]
        assert ngl == "0"
        assert "--ctx-size" in cpu_args
        assert "--no-mmproj-offload" in cpu_args
        assert "--no-kv-offload" in cpu_args or "-nkvo" in cpu_args

    def test_llama_server_cpu_differs_from_gpu(self, cfg):
        assert cfg.LLAMA_SERVER_ARGS_CPU != cfg.LLAMA_SERVER_ARGS
        gpu_ngl = cfg.LLAMA_SERVER_ARGS[cfg.LLAMA_SERVER_ARGS.index("--n-gpu-layers") + 1]
        assert gpu_ngl != "0"

    def test_image_models_nonempty(self, cfg):
        assert isinstance(cfg.IMAGE_MODELS, dict)
        assert len(cfg.IMAGE_MODELS) >= 1

    def test_token_costs(self, cfg):
        assert cfg.IMAGE_TOKEN_COST > 0
        assert cfg.AUDIO_TOKEN_COST > 0
        assert cfg.PER_MESSAGE_OVERHEAD > 0

    def test_directories_defined(self, cfg):
        assert cfg.FILES_DIR
        assert cfg.SESSIONS_FILE.endswith("sessions.json")
        assert cfg.UPLOADS_DIR
        assert cfg.IMG_PATH
        assert cfg.TASKS_DB.endswith("tasks.db")

    def test_sessions_in_dedicated_dir(self, cfg):
        assert cfg.SESSIONS_DIR == os.path.join(cfg.FILES_DIR, "session")
        assert cfg.SESSIONS_FILE == os.path.join(cfg.SESSIONS_DIR, "sessions.json")
