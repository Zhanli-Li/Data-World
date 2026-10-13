"""出题阶段编排：``<world>/`` → ``tasks.json``。

每道题一次独立的长会话：采样目标 → 只读世界副本 → agent 自由探索 → 交付
标记块 → 契约校验 → 失败把错误回喂重试。世界目录本身从不被写入。

**只读副本是结构保证，不是事后检测。** agent 需要在世界里跑脚本、建中间文件，
所以不能直接把世界目录挂给它；但副本一旦可写，agent 就可能改掉观测数据，让
rubric 里的数值与答题者看到的数据不一致。历史实现的做法是拷可写副本 +
sha256 清单事后比对 + 发现污染就重拷（``world_contamination`` 字段），那是用检
测弥补结构缺陷。此处改为拷完即 ``chmod`` 去掉写位：agent 的工作目录另设在
副本之外，写世界会直接 ``EACCES``，污染在物理上不可能发生，清单与重拷逻辑一并
消失。

重试语义遵循 AGENTS.md：``sdk``/``runner`` 负责瞬时传输错误重试，本模块负责
**产物非法**的重试，且把校验错误文本喂回下一次 prompt（约定同 ``data_world.pipeline``）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from data_world.fs import atomic_write_json

from .. import TaskGenError
from ..contract import (WORLD_COPY as _WORLD_COPY, attach_identity, build_rubric,
                        check_no_leak, extract_task, validate_task)
from .prompts import build_synth_prompt
from .sampler import select_targets

#: 产物非法时的重试次数（错误回喂）。
SYNTH_RETRIES = 2

#: 世界副本目录名（位于世界产物目录下，出题结束后保留以便复核 rubric 数值）。
#: 定义在 contract 里：它是产物布局契约的一部分，下游评测工具按这个名字回头找 env/。
WORLD_COPY = _WORLD_COPY

#: 不拷进副本的顶层条目：``trajectories/`` 是世界生成过程的会话记录，与出题
#: 无关（且可能很大），出现在副本里只会让出题 agent 分心去读别的 agent 的过程。
COPY_EXCLUDE = ("trajectories",)


# ── 只读世界副本 ──────────────────────────────────────────────────────────────

def _set_read_only(root: Path) -> None:
    """递归去掉写位：文件 ``r-x``、目录 ``r-x``（目录保留 x 才能进入与列目录）。

    自底向上处理：先改文件再改目录，否则目录一旦只读就无法再修改其中的条目。

    注意：权限位对 root 无效（root 无视写位）。出题按设计以普通用户跑，故此处
    成立；若将来在 root 容器里出题，需改用 bind-mount ``ro`` 或独立 uid 才能
    维持同一保证。
    """
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        mode = path.stat().st_mode
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    root.chmod(root.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _restore_writable(root: Path) -> None:
    """恢复写位，供重拷/清理使用（只读目录无法被 ``shutil.rmtree`` 删除）。"""
    root.chmod(root.stat().st_mode | stat.S_IWUSR)
    for path in root.rglob("*"):
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


def make_world_copy(world_dir: Path, destination: Path) -> Path:
    """拷一份完整世界副本并置为只读，返回副本路径。

    副本包含 ``env/``、``WORLD.md``、``dag.json`` 与 ``.build/``：出题 agent 需要
    ``.build/`` 里的 canonical truth 与审计证据来核验自己写进评分点的数值，而答题者
    只会拿到 ``env/``。
    """
    if destination.exists():
        _restore_writable(destination)
        shutil.rmtree(destination)
    shutil.copytree(world_dir, destination, symlinks=False,
                    ignore=shutil.ignore_patterns(*COPY_EXCLUDE))
    _set_read_only(destination)
    return destination


# ── 单题出题 ─────────────────────────────────────────────────────────────────

def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def synth_one(material: Dict[str, Any], world: str, out_dir: Path, world_copy: Path,
              runner: Any, retries: int = SYNTH_RETRIES) -> Dict[str, Any]:
    """出一道题：会话 → 取标记块 → 校验 → 非法则回喂错误重试。

    每次尝试用独立的工作目录（``run_dir``），它在只读副本之外，agent 的脚本与
    中间文件都落在这里。返回的 result 始终含 ``ok``；失败时含 ``errors``。
    """
    task_id = material["task_id"]
    last: Dict[str, Any] = {"ok": False, "errors": ["not started"]}
    #: 上一次的**校验**错误；仅产物非法时非空，会话级失败不回喂（见下）。
    feedback: List[str] = []

    for attempt in range(retries + 1):
        run_dir = out_dir / task_id / (f"attempt{attempt}" if attempt else "run")
        run_dir.mkdir(parents=True, exist_ok=True)
        # 提示词要带上副本与工作目录的绝对路径（agent 的 cwd 在副本之外，裸文件名
        # 无处可读），而 run_dir 每次尝试都不同，故必须在循环内重建。
        base_prompt = build_synth_prompt(material, world_copy.resolve(),
                                        run_dir.resolve())
        prompt = _retry_prompt(base_prompt, feedback) if feedback else base_prompt
        started = time.time()
        ok, message, raw = runner.run(prompt, cwd=run_dir, add_dirs=[world_copy])
        elapsed = round(time.time() - started, 1)
        _write_text(run_dir / "final_message.txt", message or "")
        if raw is not None:
            atomic_write_json(run_dir / "session.json", raw)

        result: Dict[str, Any] = {
            "task_id": task_id, "world": world, "attempt": attempt,
            "seconds": elapsed, "agent_ok": ok,
            "num_turns": (raw or {}).get("num_turns"),
        }
        if not ok:
            result.update({"ok": False, "errors": [f"会话失败: {(message or '')[-300:]}"]})
            last = result
            feedback = []  # 会话级失败与产物无关，不回喂校验错误
            continue

        task, errors = validate_task(extract_task(message or ""))
        if task is None:
            result.update({"ok": False, "errors": errors or ["未找到合法交付标记块"]})
            last = result
            feedback = result["errors"]
            continue
        # 身份字段单向注入，不让 agent 抄：见 contract.attach_identity。
        task = attach_identity(task, material)

        leaks = check_no_leak(world, task, task["task_prompt"])
        result.update({"ok": True, "task": task, "leaks": leaks,
                       "n_criteria": len(build_rubric(world, task)["criteria"])})
        return result

    return last


def _retry_prompt(base_prompt: str, errors: List[str]) -> str:
    """把校验错误回喂给下一次尝试（约定同 ``data_world.pipeline``）。"""
    listed = "\n".join(f"- {error}" for error in errors)
    return (f"{base_prompt}\n\n[上一次交付被拒，请修正后重新交付]\n{listed}\n"
            "请重新完成探索与核验（可复用上次结论），并严格按交付格式输出。")


# ── 单个世界 ─────────────────────────────────────────────────────────────────

def synth_world(world: str, world_dir: Path, out_dir: Path, runner: Any,
                task_ids: Optional[List[str]] = None, resume: bool = True,
                retries: int = SYNTH_RETRIES, keep_copy: bool = True,
                ) -> Dict[str, Any]:
    """给一个世界出全部题目，写 ``tasks.json`` 与 ``report.json``。

    ``resume=True`` 时已成功的题跳过（按 ``tasks.json`` 里已有 task_id 判断），
    可断点续跑——采样是确定性的，重跑落到同一批目标。
    """
    if not (world_dir / "dag.json").is_file():
        raise TaskGenError(f"不是有效的世界目录（缺 dag.json）: {world_dir}")
    materials = {item["task_id"]: item for item in select_targets(world_dir)}
    if task_ids:
        unknown = [tid for tid in task_ids if tid not in materials]
        if unknown:
            raise TaskGenError(f"{world}: 未知 task_id {unknown}，"
                               f"可选 {sorted(materials)}")
        materials = {tid: materials[tid] for tid in task_ids}

    out_dir.mkdir(parents=True, exist_ok=True)
    done = _load_done(out_dir / "tasks.json") if resume else {}
    pending = [material for tid, material in materials.items() if tid not in done]
    if not pending:
        print(f"  [{world}] 全部 {len(done)} 题已完成，跳过")
        return _write_report(world, out_dir, done, [], skipped=True)

    world_copy = make_world_copy(world_dir, out_dir / WORLD_COPY)
    failures: List[Dict[str, Any]] = []
    try:
        for material in pending:
            print(f"  [{world}/{material['task_id']}] "
                  f"target={material['target']} {material['degree_kind']}="
                  f"{material['degree']} depth={material['depth']}", flush=True)
            result = synth_one(material, world, out_dir, world_copy, runner, retries)
            if result["ok"]:
                done[result["task_id"]] = result["task"]
                _write_tasks(out_dir / "tasks.json", world, done)  # 逐题落盘，便于续跑
                note = f" 泄漏警告×{len(result['leaks'])}" if result.get("leaks") else ""
                print(f"    ok  criteria={result['n_criteria']} "
                      f"{result['seconds']}s{note}", flush=True)
            else:
                failures.append(result)
                print(f"    fail {result['errors'][:2]}", flush=True)
    finally:
        if not keep_copy:
            _restore_writable(world_copy)
            shutil.rmtree(world_copy, ignore_errors=True)

    return _write_report(world, out_dir, done, failures)


def _load_done(tasks_path: Path) -> Dict[str, Dict[str, Any]]:
    if not tasks_path.is_file():
        return {}
    try:
        payload = json.loads(tasks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {task["task_id"]: task for task in payload.get("tasks", [])
            if isinstance(task, dict) and task.get("task_id")}


def _write_tasks(tasks_path: Path, world: str, done: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_json(tasks_path, {
        "world": world,
        "tasks": [done[tid] for tid in sorted(done)],
    })


def _write_report(world: str, out_dir: Path, done: Dict[str, Dict[str, Any]],
                  failures: List[Dict[str, Any]], skipped: bool = False,
                  ) -> Dict[str, Any]:
    report = {
        "world": world,
        "n_tasks": len(done),
        "task_ids": sorted(done),
        "n_failed": len(failures),
        "failures": [{k: v for k, v in f.items() if k != "task"} for f in failures],
        "skipped": skipped,
    }
    atomic_write_json(out_dir / "report.json", report)
    return report


# ── 批量 ─────────────────────────────────────────────────────────────────────

def discover_worlds(root: Path) -> List[Path]:
    """在 ``root`` 下找出所有世界目录（含 ``dag.json`` 且含 ``env/``）。

    ``root`` 本身就是一个世界时直接返回它，便于单世界调试。
    """
    root = Path(root)
    if (root / "dag.json").is_file():
        return [root]
    if not root.is_dir():
        raise TaskGenError(f"世界目录不存在: {root}")
    return sorted(path for path in root.iterdir()
                  if path.is_dir() and (path / "dag.json").is_file())


def synth(args: argparse.Namespace, runner: Any) -> int:
    """批量出题；返回失败世界数（CLI 退出码用）。"""
    worlds = discover_worlds(Path(args.worlds))
    if not worlds:
        raise TaskGenError(f"{args.worlds} 下没有找到世界目录（需含 dag.json）")
    if args.limit:
        worlds = worlds[:args.limit]
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"[synth] {len(worlds)} 个世界 → {out_root}", flush=True)

    n_failed = 0
    for index, world_dir in enumerate(worlds, 1):
        world = world_dir.name
        print(f"[{index}/{len(worlds)}] {world}", flush=True)
        try:
            report = synth_world(
                world, world_dir, out_root / world, runner,
                task_ids=args.task_ids, resume=not args.force,
                retries=args.retries, keep_copy=not args.no_keep_copy)
        except TaskGenError as exc:
            print(f"  [error] {world}: {exc}", flush=True)
            n_failed += 1
            continue
        if report["n_failed"]:
            n_failed += 1
    print(f"[synth] 完成，{n_failed}/{len(worlds)} 个世界有失败", flush=True)
    return n_failed
