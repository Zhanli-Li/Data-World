from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import WorldGenError, prompts
from .dag import ancestors_for_layer, incoming_edges, topo_layers, validate_and_normalize_dag
from .fs import atomic_write_json, ensure_inside, list_relative_files, read_json, sha256_file, slugify
from .sdk import ClaudeRunner, LLMClient
from .trajectory import CCRProxy, write_single_turn_jsonl, write_stage_jsonl


class DataWorldPipeline:
    """完整生成编排器。

    参考 terminal-bench 的组织方式：一个主编排器持有运行状态，DAG、IO、SDK、提示词
    作为少量独立能力模块；阶段校验紧挨阶段实现，避免 context/stages/validation 多层跳转。
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.input_dag = Path(args.dag).expanduser().resolve()
        if not self.input_dag.is_file():
            raise WorldGenError(f"DAG 文件不存在: {self.input_dag}")
        self.dag = validate_and_normalize_dag(read_json(self.input_dag))
        self.layers = topo_layers(self.dag)
        self.dag_hash = sha256_file(self.input_dag)
        self.root = Path(args.output).expanduser().resolve()
        if args.name:
            self.root /= slugify(args.name)
        self.env = self.root / "env"
        self.world_md = self.root / "WORLD.md"
        self.dag_copy = self.root / "dag.json"
        self.build = self.root / ".build"
        self.contract = self.build / "world_contract.json"
        self.progress_path = self.build / "progress.json"
        self.layer_root = self.build / "layers"
        self.projection = self.build / "projection"
        self.audit = self.build / "audit"
        self.progress: Dict[str, Any] = {}
        # 本次 build 的会话亲和 key：贯穿 contract（LLMClient）与工具阶段（CCR），
        # 让所有请求命中代理侧 default 亲和并粘到同一上游渠道，复用前缀 cache。
        self.affinity_key = uuid.uuid4().hex
        self.runner = ClaudeRunner(args.claude_bin, args.model, args.endpoint, args.auth_token,
                                   args.timeout, args.max_turns, args.sdk_retries, args.verbose)
        self.llm = LLMClient(args.endpoint, args.auth_token, args.contract_model,
                             args.timeout, args.sdk_retries, args.verbose,
                             affinity_key=self.affinity_key)
        self.trajectories = self.root / "trajectories"
        self.log_trajectories = bool(getattr(args, "log_trajectories", False))
        self.proxy: Optional[CCRProxy] = None
        self._ccr_home: Optional[str] = None
        if self.log_trajectories:
            openai_base = (args.endpoint or "").rstrip("/").removesuffix("/anthropic")
            if not openai_base:
                raise WorldGenError("--log-trajectories 需要可用的 --endpoint（或 ANTHROPIC_BASE_URL）")
            self._ccr_home = tempfile.mkdtemp(prefix="dw_ccr_")
            self.proxy = CCRProxy(args.ccr_bin, openai_base, args.auth_token,
                                  args.trajectory_model, Path(self._ccr_home),
                                  args.ccr_port, args.verbose, affinity_key=self.affinity_key)

    def run(self) -> None:
        self._initialize()
        print(f"[world] {self.root}")
        print(f"[dag] {len(self.dag['nodes'])} nodes / {len(self.dag['edges'])} edges / {len(self.layers)} layers")
        print("[layers] " + " | ".join(f"L{i}:{labels}" for i, labels in enumerate(self.layers)))
        self._build_contract()
        try:
            if self.proxy is not None:
                self.proxy.start()
                self.runner.endpoint = self.proxy.base_url  # SDK 三步经 CCR
            for index, labels in enumerate(self.layers):
                self._build_layer(index, labels)
            self._project_environment()
            self._audit_world()
        finally:
            if self.proxy is not None:
                self.proxy.stop()
                if self._ccr_home:
                    shutil.rmtree(self._ccr_home, ignore_errors=True)
        self._validate_final_world()
        print(f"\n完成: {self.root}\n  env: {self.env}\n  world details: {self.world_md}\n  original DAG: {self.dag_copy}")

    def _dump_stage_trajectory(self, snapshot: Optional[Dict[str, int]], stage: str) -> None:
        """把某 SDK 阶段新增的 CCR 日志重组成 trajectories/<stage>.jsonl。"""
        if self.proxy is None or snapshot is None:
            return
        lines = self.proxy.lines_since(snapshot)
        if not lines:
            return
        out_path = self.trajectories / f"{stage}.jsonl"
        count = write_stage_jsonl(lines, out_path, stage, self.args.trajectory_model)
        if self.args.verbose:
            print(f"    [trajectory] {stage}: 写出 {count} 条对话 -> {out_path}", flush=True)

    # ---------- setup / progress ----------

    def _initialize(self) -> None:
        if self.root.exists() and any(self.root.iterdir()) and not self.args.resume and not self.args.force:
            raise WorldGenError(f"输出目录非空: {self.root}；使用 --resume 或 --force")
        if self.args.force and self.root.exists():
            shutil.rmtree(self.root)
        self.env.mkdir(parents=True, exist_ok=True)
        self.layer_root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.input_dag, self.dag_copy)
        if self.args.resume and self.progress_path.exists():
            self.progress = read_json(self.progress_path)
            if self.progress.get("dag_sha256") != self.dag_hash or self.progress.get("layers") != self.layers:
                raise WorldGenError("--resume 时 DAG 与已有进度不一致")
            self._reset_failed_stages()
            return
        self.progress = {
            "schema_version": 1, "dag_sha256": self.dag_hash, "input_dag": str(self.input_dag),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "layers": self.layers,
            "contract": "pending", "layer_status": {str(i): "pending" for i in range(len(self.layers))},
            "layer_attempts": {str(i): 0 for i in range(len(self.layers))},
            "projection": "pending", "projection_attempts": 0,
            "audit": "pending", "audit_attempts": 0,
        }
        self._save_progress()

    def _reset_failed_stages(self) -> None:
        # A new --resume invocation grants a fresh per-invocation retry budget:
        # stages left "failed" (retries exhausted) revert to "pending" so they run again.
        if self.progress.get("contract") == "failed":
            self.progress["contract"] = "pending"
        for key, status in self.progress.get("layer_status", {}).items():
            if status == "failed":
                self.progress["layer_status"][key] = "pending"
                self.progress["layer_attempts"][key] = 0
        for name in ("projection", "audit"):
            if self.progress.get(name) == "failed":
                self.progress[name] = "pending"
                self.progress[f"{name}_attempts"] = 0
        self._save_progress()

    def _save_progress(self) -> None:
        self.progress["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_write_json(self.progress_path, self.progress)

    def _brief(self) -> Dict[str, Any]:
        return {"persona": self.args.persona, "seed": self.args.seed}

    def _layer_dir(self, index: int) -> Path:
        return self.layer_root / f"layer_{index:02d}"

    def _layer_result(self, index: int) -> Path:
        return self._layer_dir(index) / "layer_result.json"

    def _layer_doc(self, index: int) -> Path:
        """本层的叙事自述（设计理由与机制），与 layer_result.json 的账本互补。"""
        return self._layer_dir(index) / f"layer_{index:02d}.md"

    # ---------- world contract ----------

    def _build_contract(self) -> None:
        if self.progress.get("contract") == "done" and self.contract.is_file():
            print("[contract] resume: done")
            self._validate_contract(read_json(self.contract))
            return
        print("[contract] 一次 LLM 调用编译统一世界合同")
        self.build.mkdir(parents=True, exist_ok=True)
        error = ""
        for attempt in range(self.args.stage_retries + 1):
            print(f"[contract] attempt={attempt + 1}")
            try:
                prompt = prompts.contract(self._brief(), self.dag, self.layers, error)
                value, response_text = self.llm.complete_with_trace(prompt)
                self._validate_contract(value)
                atomic_write_json(self.contract, value)
                if self.log_trajectories:
                    write_single_turn_jsonl(prompt, response_text,
                                            self.trajectories / "contract.jsonl",
                                            "contract", self.args.contract_model)
                self.progress["contract"] = "done"
                self._save_progress()
                return
            except (WorldGenError, json.JSONDecodeError) as exc:
                error = str(exc)
                self.progress["contract"] = "failed"
                self._save_progress()
                if attempt >= self.args.stage_retries:
                    raise WorldGenError(f"world contract 失败: {error[-1000:]}") from exc
                print(f"    [contract] 校验失败，将定向重试: {error}")

    def _validate_contract(self, value: Any) -> None:
        if not isinstance(value, dict):
            raise WorldGenError("world_contract.json 根不是 object")
        required = {"world_identity", "timeline", "entities", "mechanisms", "observation_systems", "datasets", "causal_phenomena", "layer_contracts"}
        missing = sorted(required - value.keys())
        if missing:
            raise WorldGenError(f"world_contract.json 缺少字段: {missing}")
        contracts = value.get("layer_contracts")
        if not isinstance(contracts, list) or len(contracts) != len(self.layers):
            raise WorldGenError(f"layer_contracts 数量应为 {len(self.layers)}")
        covered = {str(label) for item in contracts if isinstance(item, dict)
                   for label in item.get("nodes", []) if isinstance(item.get("nodes"), list)}
        expected = {str(node["label"]) for node in self.dag["nodes"]}
        if covered != expected:
            raise WorldGenError(f"layer_contracts 节点覆盖不一致: missing={sorted(expected-covered)}, extra={sorted(covered-expected)}")

    # ---------- one SDK per topological layer ----------

    def _build_layer(self, index: int, labels: List[str]) -> None:
        key, result = str(index), self._layer_result(index)
        if self.progress["layer_status"].get(key) == "done" and result.is_file():
            print(f"[layer {index}] resume: done {labels}")
            self._validate_layer(index, labels, read_json(result))
            return
        if any(self.progress["layer_status"].get(str(i)) != "done" for i in range(index)):
            raise WorldGenError(f"Layer {index} 的父层尚未完成")
        directory = self._layer_dir(index)
        directory.mkdir(parents=True, exist_ok=True)
        attempts = int(self.progress["layer_attempts"].get(key, 0))
        error = ""
        snapshot = self.proxy.log_snapshot() if self.proxy is not None else None
        while attempts <= self.args.stage_retries:
            print(f"[layer {index}/{len(self.layers)-1}] 一次 SDK 联合生成 {labels}，attempt={attempts + 1}")
            node_map = {str(node["label"]): node for node in self.dag["nodes"]}
            prompt = prompts.layer(
                index, [node_map[x] for x in labels], incoming_edges(self.dag, labels),
                ancestors_for_layer(self.dag, labels),
                [str(self._layer_result(i)) for i in range(index) if self._layer_result(i).is_file()],
                self.contract, self.dag_copy, directory, result, self._layer_doc(index),
                self.env, self.args.seed + index, error,
            )
            ok, message, _ = self.runner.run(prompt, directory, [self.build, self.root])
            try:
                if not ok or not result.is_file():
                    raise WorldGenError(f"SDK/产物失败: {message[-1200:]}")
                self._validate_layer(index, labels, read_json(result))
                self.progress["layer_status"][key] = "done"
                self._save_progress()
                self._dump_stage_trajectory(snapshot, f"layer_{index:02d}")
                return
            except (WorldGenError, json.JSONDecodeError, OSError) as exc:
                attempts += 1
                error = str(exc)
                self.progress["layer_attempts"][key] = attempts
                self.progress["layer_status"][key] = "failed"
                self._save_progress()
                if attempts > self.args.stage_retries:
                    raise WorldGenError(f"Layer {index} 耗尽重试: {error}") from exc
                print(f"    [layer {index}] 校验失败，将定向重试: {error}")

    def _validate_layer(self, index: int, labels: List[str], value: Any) -> None:
        if not isinstance(value, dict) or value.get("layer") != index or value.get("status") != "complete":
            raise WorldGenError("layer_result 的 layer/status 无效")
        if not isinstance(value.get("nodes"), list) or set(map(str, value["nodes"])) != set(labels):
            raise WorldGenError(f"layer_result.nodes 不匹配: {value.get('nodes')}")
        files = value.get("state_files")
        if not isinstance(files, list) or not files:
            raise WorldGenError("state_files 必须非空")
        root = self._layer_dir(index)
        for item in files:
            if not isinstance(item, str) or not item.strip():
                raise WorldGenError(f"非法 state_files 项: {item!r}")
            path = (root / item).resolve()
            ensure_inside(path, root)
            if not path.is_file() or path.stat().st_size == 0 or path.suffix.lower() in {".py", ".pyc", ".sh"}:
                raise WorldGenError(f"canonical state 缺失、为空或是脚本: {path}")
        outputs = value.get("node_outputs")
        if not isinstance(outputs, dict) or set(map(str, outputs)) != set(labels):
            raise WorldGenError("node_outputs 未逐一覆盖当前层")
        expected_edges = incoming_edges(self.dag, labels)
        checks = value.get("edge_checks", [])
        if expected_edges and (not isinstance(checks, list) or len(checks) < len(expected_edges)):
            raise WorldGenError(f"edge_checks 少于入边数 {len(expected_edges)}")
        self._require_doc(self._layer_doc(index), f"Layer {index} 的 canonical state 与 layer_result.json 已生成")

    #: 叙事自述的最小字节数。低于此视为敷衍/未写——实测自发写出的同类文档约 6.7KB，
    #: 门槛留足一个数量级的余量，只拦"根本没写"，不对写作质量设卡。
    DOC_MIN_BYTES = 800

    def _require_doc(self, path: Path, done_hint: str) -> None:
        """断言阶段自述已写。

        ``done_hint`` 会进错误文本：这个断言失败时数据往往是对的，只差一份文档，
        定向重试必须明说"补写即可"，否则模型会重造一遍 canonical state——那是几十
        分钟的算力，而缺的只是一个文件。
        """
        size = path.stat().st_size if path.is_file() else -1
        if size < self.DOC_MIN_BYTES:
            state = "不存在" if size < 0 else f"只有 {size} 字节"
            raise WorldGenError(
                f"缺少阶段自述 {path}（{state}，需 ≥{self.DOC_MIN_BYTES} 字节）。"
                f"{done_hint}，无需重新生成数据；请只补写这份 markdown 自述："
                "设计意图、机制、放弃的取舍与下游注意事项。")

    # ---------- projection / audit ----------

    def _project_environment(self) -> None:
        result = self.projection / "projection_result.json"
        if self.progress.get("projection") == "done" and result.is_file():
            print("[projection] resume: done")
            self._validate_projection(read_json(result))
            return
        self.projection.mkdir(parents=True, exist_ok=True)
        layer_results = [str(self._layer_result(i)) for i in range(len(self.layers))]
        self._run_stage(
            "projection", "projection_attempts",
            lambda error: prompts.projection(self.contract, layer_results, self.projection,
                                             self.projection / "projection.md", self.env, error),
            lambda prompt: self.runner.run(prompt, self.projection, [self.build, self.env, self.root]),
            lambda: self._validate_projection(read_json(result)) if result.is_file() else self._fail("没有写出 projection_result.json"),
        )

    def _validate_projection(self, value: Any) -> None:
        if not isinstance(value, dict) or value.get("status") != "complete":
            raise WorldGenError("projection_result 无效")
        actual = list_relative_files(self.env)
        declared = value.get("env_files")
        if not isinstance(declared, list) or sorted(str(x).replace("\\", "/") for x in declared) != actual:
            raise WorldGenError(f"env_files 与实际 env 不一致: declared={declared}, actual={actual}")
        if not actual or not isinstance(value.get("datasets"), list) or not value["datasets"]:
            raise WorldGenError("env 或 datasets 为空")
        # 刻意不把 .md 列入 forbidden：release notes、codebook、协议说明这类 in-world
        # 物料本就是真实数据室的一部分。挡住"投影自述泄漏进 env"靠提示词里的贴身禁令。
        forbidden = {".py", ".pyc", ".sh", ".ipynb"}
        for rel in actual:
            path = self.env / rel
            if path.stat().st_size == 0 or path.suffix.lower() in forbidden or "__pycache__" in path.parts:
                raise WorldGenError(f"env 文件为空或属于过程产物: {rel}")
        self._require_doc(self.projection / "projection.md", "env 数据与 projection_result.json 已生成")

    def _audit_world(self) -> None:
        result = self.audit / "audit_result.json"
        if self.progress.get("audit") == "done" and self.world_md.is_file() and result.is_file():
            print("[audit] resume: done")
            return
        self.audit.mkdir(parents=True, exist_ok=True)
        self._run_stage(
            "audit", "audit_attempts",
            lambda error: prompts.audit(self.dag_copy, self.contract, self.layer_root,
                                        self.projection / "projection_result.json", self.env,
                                        self.world_md, self.audit, error),
            lambda prompt: self.runner.run(prompt, self.audit, [self.build, self.env, self.root]),
            lambda: self._validate_audit(result),
        )

    def _validate_audit(self, result: Path) -> None:
        if not self.world_md.is_file() or self.world_md.stat().st_size < 500 or not result.is_file():
            raise WorldGenError("WORLD.md 或 audit_result.json 缺失")
        value = read_json(result)
        if not isinstance(value, dict) or value.get("status") != "complete":
            raise WorldGenError("audit_result 未 complete")

    def _run_stage(self, name: str, attempts_key: str, prompt: Callable[[str], str],
                   invoke: Callable[[str], Tuple[bool, str, Optional[Dict[str, Any]]]],
                   validate: Callable[[], None]) -> None:
        attempts, error = int(self.progress.get(attempts_key, 0)), ""
        snapshot = self.proxy.log_snapshot() if self.proxy is not None else None
        while attempts <= self.args.stage_retries:
            print(f"[{name}] attempt={attempts + 1}")
            ok, message, _ = invoke(prompt(error))
            try:
                if not ok:
                    raise WorldGenError(f"SDK 失败: {message[-1200:]}")
                validate()
                self.progress[name] = "done"
                self._save_progress()
                self._dump_stage_trajectory(snapshot, name)
                return
            except (WorldGenError, json.JSONDecodeError, OSError) as exc:
                attempts += 1
                error = str(exc)
                self.progress[attempts_key], self.progress[name] = attempts, "failed"
                self._save_progress()
                if attempts > self.args.stage_retries:
                    raise WorldGenError(f"{name} 耗尽重试: {error}") from exc
                print(f"    [{name}] 校验失败，将定向重试: {error}")

    @staticmethod
    def _fail(message: str) -> None:
        raise WorldGenError(message)

    # ---------- final validation ----------

    def _validate_final_world(self) -> None:
        if not self.dag_copy.is_file() or sha256_file(self.dag_copy) != self.dag_hash:
            raise WorldGenError("dag.json 不是输入 DAG 的原始副本")
        if not self.world_md.is_file() or self.world_md.stat().st_size == 0:
            raise WorldGenError("WORLD.md 缺失")
        files = list_relative_files(self.env)
        if not files:
            raise WorldGenError("env 为空")
        bad = [rel for rel in files if Path(rel).suffix.lower() in {".py", ".pyc", ".sh", ".ipynb"}]
        if bad:
            raise WorldGenError(f"env 存在过程脚本: {bad}")
