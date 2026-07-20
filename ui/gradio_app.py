"""
个人数字分身 — 前端（对齐全部后端接口）
"""
import json
import requests
import httpx
from pathlib import Path
import gradio as gr
from config.settings import settings

API = f"http://127.0.0.1:{settings.port}"
AGENT_MODE = "chat"


# ══════════════════════════════════════
# 后端 API 调用
# ══════════════════════════════════════

def _headers(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def do_login(username, password):
    try:
        r = requests.post(f"{API}/api/auth/login",
                          json={"username": username, "password": password}, timeout=10)
        if r.status_code == 200:
            return r.json()
        return {"error": r.json().get("detail", "登录失败")}
    except Exception as e:
        return {"error": str(e)}


def do_get_user_info(token):
    try:
        r = requests.get(f"{API}/api/auth/me", headers=_headers(token), timeout=10)
        if r.status_code == 200:
            return r.json()
        return {"error": r.text}
    except Exception as e:
        return {"error": str(e)}


def do_kb_files(token):
    try:
        r = requests.get(f"{API}/api/kb/files", headers=_headers(token), timeout=10)
        if r.status_code == 200:
            return r.json()
        return {"error": r.text}
    except Exception as e:
        return {"error": str(e)}


def do_upload(token, file_paths):
    try:
        files = []
        for p in file_paths:
            files.append(("files", (Path(p).name, open(p, "rb"))))
        r = requests.post(f"{API}/api/kb/upload", files=files,
                          headers=_headers(token), timeout=120)
        for _, (_, fh) in files:
            fh.close()
        return r.json() if r.status_code == 200 else {"error": r.text}
    except Exception as e:
        return {"error": str(e)}


def do_delete_file(token, file_id):
    try:
        r = requests.delete(f"{API}/api/kb/files/{file_id}",
                            headers=_headers(token), timeout=10)
        return r.json() if r.status_code == 200 else {"error": r.text}
    except Exception as e:
        return {"error": str(e)}


def do_clear_kb(token):
    try:
        r = requests.delete(f"{API}/api/kb/clear", headers=_headers(token), timeout=10)
        return r.json() if r.status_code == 200 else {"error": r.text}
    except Exception as e:
        return {"error": str(e)}


def do_rebuild_kb(token):
    try:
        r = requests.post(f"{API}/api/kb/rebuild", headers=_headers(token), timeout=120)
        return r.json() if r.status_code == 200 else {"error": r.text}
    except Exception as e:
        return {"error": str(e)}


def do_chat_history(token, limit=50, offset=0):
    try:
        r = requests.get(f"{API}/api/chat/history",
                         params={"limit": limit, "offset": offset},
                         headers=_headers(token), timeout=10)
        if r.status_code == 200:
            return r.json()
        return {"error": r.text}
    except Exception as e:
        return {"error": str(e)}


def do_eval_reports(token):
    try:
        r = requests.get(f"{API}/api/eval/reports", headers=_headers(token), timeout=10)
        if r.status_code == 200:
            return r.json()
        return {"error": r.text}
    except Exception as e:
        return {"error": str(e)}


def do_eval_report_detail(token, report_id):
    try:
        r = requests.get(f"{API}/api/eval/reports/{report_id}", headers=_headers(token), timeout=10)
        if r.status_code == 200:
            return r.json()
        return {"error": r.text}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════
# UI 组件
# ══════════════════════════════════════

def create_gradio_app():
    st_token = gr.State(None)
    st_owner = gr.State(None)
    st_name = gr.State(None)
    st_logged = gr.State(False)
    st_user_info = gr.State({})

    with gr.Blocks(title="个人数字分身") as app:
        gr.Markdown("# 🤖 个人数字分身")
        gr.Markdown("*AI 面试助手 · 基于私有知识库的智能问答*")

        with gr.Row():
            el_status = gr.Markdown("🔴 未登录 — 无法访问知识库")
            el_user_info = gr.Markdown("")

        with gr.Tabs():
            with gr.Tab("💬 对话"):
                el_chatbot = gr.Chatbot(label="对话", height=500, layout="bubble")

                with gr.Row():
                    el_input = gr.Textbox(label="", placeholder="输入你的问题后按回车发送...",
                                          lines=1, scale=9)
                    el_send = gr.Button("发送", variant="primary", scale=1)
                    el_stop = gr.Button("停止", variant="stop", scale=1)

                with gr.Row():
                    el_clear_chat = gr.Button("清空对话", size="sm")
                    el_refresh_history = gr.Button("刷新历史", size="sm")

                with gr.Accordion("查看推理过程", open=False):
                    el_reasoning = gr.Textbox(label="", lines=5, max_lines=15, interactive=False)

                with gr.Accordion("对话历史", open=False):
                    el_history_list = gr.Dataframe(
                        headers=["时间", "问题", "回答"],
                        label="历史记录",
                        wrap=True,
                        interactive=False,
                    )

            with gr.Tab("📚 知识库管理"):
                with gr.Row():
                    el_file = gr.File(label="选择文档（多选）", file_count="multiple", scale=3,
                                      file_types=[".pdf", ".docx", ".xlsx", ".txt", ".md", ".py", ".json", ".zip",
                                                  ".png", ".jpg"])
                    el_upload_btn = gr.Button("上传并入库", variant="primary", scale=1)

                with gr.Row():
                    el_refresh_btn = gr.Button("刷新文件列表", size="sm")
                    el_del_id = gr.Number(label="要删除的文件ID", value=0, precision=0)
                    el_del_btn = gr.Button("删除该文件", variant="stop", size="sm")
                    el_clear_btn = gr.Button("清空知识库", variant="stop", size="sm")
                    el_rebuild_btn = gr.Button("重建向量索引", size="sm")

                el_kb_msg = gr.Markdown("")
                el_kb_files = gr.HTML("<p>点击「刷新文件列表」查看</p>")

            with gr.Tab("📊 评测"):
                with gr.Row():
                    el_eval_file = gr.File(label="上传评测集（JSON格式）", file_types=[".json"], scale=3)
                    el_eval_btn = gr.Button("开始自动评测", variant="primary", scale=1)

                el_eval_msg = gr.Markdown("")

                with gr.Row():
                    el_eval_table = gr.Dataframe(
                        headers=["题号", "问题", "匹配分", "幻觉", "检索质量", "详情"],
                        label="逐题结果",
                        wrap=True,
                    )

                with gr.Row():
                    el_report_refresh = gr.Button("刷新报告列表", size="sm")
                    el_report_id = gr.Number(label="查看报告ID", value=0, precision=0)
                    el_report_detail_btn = gr.Button("查看详情", size="sm")

                with gr.Accordion("评测报告列表", open=False):
                    el_report_list = gr.Dataframe(
                        headers=["ID", "测试集", "总题数", "准确率", "幻觉率", "创建时间"],
                        label="报告记录",
                        wrap=True,
                        interactive=False,
                    )

                with gr.Accordion("报告详情", open=False):
                    el_report_detail = gr.Textbox(label="", lines=8, interactive=False)

        # ══════════════════════════════════
        # 事件处理
        # ══════════════════════════════════

        def handle_login(u, p):
            r = do_login(u, p)
            if "error" in r:
                return (None, None, None, False, {},
                        f"❌ {r['error']}",
                        "🔴 未登录 — 无法访问知识库",
                        "")
            user_info = do_get_user_info(r["access_token"])
            return (r["access_token"], r["owner_id"], r["username"], True, user_info,
                    f"✅ 已登录：**{r['username']}**",
                    f"🟢 已登录 — 可以使用知识库",
                    f"👤 用户ID: {user_info.get('id','')} | 注册时间: {str(user_info.get('created_at',''))[:10]}")

        def handle_logout():
            return (None, None, None, False, {},
                    "🔴 未登录 — 无法访问知识库",
                    "")

        with gr.Row():
            el_user = gr.Textbox(label="用户名", value=settings.admin_username, scale=2)
            el_pass = gr.Textbox(label="密码", type="password", scale=2)
            el_login_btn = gr.Button("登录", variant="primary", scale=1)
            el_logout_btn = gr.Button("退出", variant="secondary", scale=1)

        el_login_msg = gr.Markdown("")

        el_login_btn.click(
            fn=handle_login,
            inputs=[el_user, el_pass],
            outputs=[st_token, st_owner, st_name, st_logged, st_user_info,
                     el_login_msg, el_status, el_user_info])
        el_pass.submit(
            fn=handle_login,
            inputs=[el_user, el_pass],
            outputs=[st_token, st_owner, st_name, st_logged, st_user_info,
                     el_login_msg, el_status, el_user_info])
        el_logout_btn.click(
            fn=handle_logout,
            inputs=[],
            outputs=[st_token, st_owner, st_name, st_logged, st_user_info,
                     el_status, el_user_info])

        async def handle_chat(message, history, token, owner, logged):
            if not message or not message.strip():
                yield history, ""
                return

            history.append({"role": "user", "content": message})

            if not logged or not token:
                history.append({
                    "role": "assistant",
                    "content": "⚠️ 请先登录再使用知识库功能。在上方输入 admin / admin123456 登录。"
                })
                yield history, "未登录"
                return

            reasoning_lines = []
            answer_parts = []

            try:
                async with httpx.AsyncClient(timeout=300.0) as client:
                    async with client.stream(
                        "POST", f"{API}/api/chat/stream",
                        json={"message": message, "agent_mode": AGENT_MODE},
                        headers=_headers(token)
                    ) as response:
                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data: "):
                                continue
                            try:
                                event = json.loads(line[6:])
                                typ = event.get("type", "")
                                content = event.get("content", "")

                                if typ == "answer":
                                    answer_parts.append(content)
                                    current = "".join(answer_parts)
                                    yield (
                                        history + [{"role": "assistant", "content": current}],
                                        "\n".join(reasoning_lines)
                                    )
                                elif typ == "reasoning":
                                    reasoning_lines.append(f"• {content}")
                                    yield (history, "\n".join(reasoning_lines))
                                elif typ == "done":
                                    break
                                elif typ == "error":
                                    answer_parts.append(f"\n\n❌ {content}")
                            except json.JSONDecodeError:
                                continue

                final_answer = "".join(answer_parts)
                if final_answer:
                    history.append({"role": "assistant", "content": final_answer})
                else:
                    history.append({"role": "assistant", "content": "（未收到回答，请重试）"})

            except Exception as e:
                history.append({"role": "assistant", "content": f"❌ 连接失败: {e}"})
                reasoning_lines.append(f"❌ {e}")

            yield (history, "\n".join(reasoning_lines))

        evt_send = el_send.click(
            fn=handle_chat,
            inputs=[el_input, el_chatbot, st_token, st_owner, st_logged],
            outputs=[el_chatbot, el_reasoning])
        evt_submit = el_input.submit(
            fn=handle_chat,
            inputs=[el_input, el_chatbot, st_token, st_owner, st_logged],
            outputs=[el_chatbot, el_reasoning])

        evt_send.then(fn=lambda: "", inputs=[], outputs=[el_input])
        evt_submit.then(fn=lambda: "", inputs=[], outputs=[el_input])

        el_stop.click(fn=None, cancels=[evt_send, evt_submit])

        el_clear_chat.click(
            fn=lambda: ([], ""),
            inputs=[],
            outputs=[el_chatbot, el_reasoning])

        def render_history(token):
            if not token:
                return []
            r = do_chat_history(token, limit=20)
            if "error" in r:
                return []
            conversations = r.get("conversations", [])
            rows = []
            for conv in conversations:
                rows.append([
                    str(conv.get("created_at", ""))[:19],
                    conv.get("question", "")[:50],
                    conv.get("answer", "")[:80],
                ])
            return rows

        el_refresh_history.click(
            fn=lambda t: render_history(t),
            inputs=[st_token],
            outputs=[el_history_list])

        def render_file_list(token):
            if not token:
                return "<p style='color:#999'>请先登录</p>"
            r = do_kb_files(token)
            if "error" in r:
                return f"<p style='color:red'>加载失败: {r['error']}</p>"
            files = r.get("files", [])
            if not files:
                return "<p style='color:#999'>📭 知识库为空，上传文档即可开始</p>"
            html_parts = []
            for f in files:
                html_parts.append(
                    f"<div style='padding:6px 10px;margin:3px 0;border:1px solid #e0e0e0;border-radius:6px;font-size:14px'>"
                    f"<b>#{f['id']}</b> {f['filename']} "
                    f"<span style='color:#888'>({round(f.get('file_size', 0) / 1024, 1)}KB, "
                    f"{f.get('chunk_count', 0)}块, {str(f.get('created_at', ''))[:10]})</span>"
                    f"</div>"
                )
            html_parts.append(
                f"<p style='margin-top:8px;color:#666;font-size:13px'>📁 共 {len(files)} 个文件 "
                f"· 🧩 共 {r.get('total_chunks', 0)} 个向量块</p>"
            )
            return "\n".join(html_parts)

        el_refresh_btn.click(
            fn=lambda t: render_file_list(t),
            inputs=[st_token],
            outputs=[el_kb_files])

        def handle_upload(files, token):
            if not files:
                return "❌ 请先选择文件", render_file_list(token)
            if not token:
                return "❌ 请先登录", render_file_list(token)
            r = do_upload(token, [f.name for f in files])
            if "error" in r:
                return f"❌ 上传失败: {r['error']}", render_file_list(token)
            return f"✅ 上传成功！共 {r.get('data', {}).get('total_chunks', 0)} 个向量块", render_file_list(token)

        el_upload_btn.click(
            fn=handle_upload,
            inputs=[el_file, st_token],
            outputs=[el_kb_msg, el_kb_files])

        def handle_delete(fid, token):
            if not fid or fid <= 0:
                return "❌ 请输入有效的文件ID", render_file_list(token)
            if not token:
                return "❌ 请先登录", render_file_list(token)
            r = do_delete_file(token, int(fid))
            if "error" in r:
                return f"❌ 删除失败: {r['error']}", render_file_list(token)
            return f"✅ {r.get('message', '删除成功')}", render_file_list(token)

        el_del_btn.click(
            fn=handle_delete,
            inputs=[el_del_id, st_token],
            outputs=[el_kb_msg, el_kb_files])

        def handle_clear(token):
            if not token:
                return "❌ 请先登录", render_file_list(token)
            r = do_clear_kb(token)
            if "error" in r:
                return f"❌ 清空失败: {r['error']}", render_file_list(token)
            return f"✅ {r.get('message', '已清空')}", render_file_list(token)

        el_clear_btn.click(
            fn=handle_clear,
            inputs=[st_token],
            outputs=[el_kb_msg, el_kb_files])

        def handle_rebuild(token):
            if not token:
                return "❌ 请先登录", render_file_list(token)
            r = do_rebuild_kb(token)
            if "error" in r:
                return f"❌ 重建失败: {r['error']}", render_file_list(token)
            return f"✅ {r.get('message', '重建完成')}", render_file_list(token)

        el_rebuild_btn.click(
            fn=handle_rebuild,
            inputs=[st_token],
            outputs=[el_kb_msg, el_kb_files])

        def handle_eval(eval_file, token):
            if not eval_file:
                return "❌ 请上传评测集 JSON 文件", gr.update(value=[])
            if not token:
                return "❌ 请先登录", gr.update(value=[])
            filename = Path(eval_file.name).name
            try:
                with open(eval_file.name, "rb") as f:
                    r = requests.post(
                        f"{API}/api/eval/testset/upload",
                        files={"file": (filename, f, "application/json")},
                        headers=_headers(token), timeout=30)
                if r.status_code != 200:
                    return f"❌ 测试集上传失败: {r.text}", gr.update(value=[])

                r = requests.post(
                    f"{API}/api/eval/run",
                    params={"testset_filename": filename},
                    headers=_headers(token), timeout=600)
                if r.status_code != 200:
                    return f"❌ 评测执行失败: {r.text}", gr.update(value=[])

                report = r.json().get("data", {})
                rows = []
                for item in report.get("results", []):
                    rows.append([
                        item.get("question_id", ""),
                        item.get("question", "")[:80],
                        f"{item.get('match_score', 0):.0f}",
                        "⚠️ 幻觉" if item.get("is_hallucination") else "✅",
                        item.get("retrieval_quality", ""),
                        (item.get("hallucination_detail", "") or "")[:100],
                    ])

                return (
                    f"✅ 评测完成：{report.get('testset_name', '')} — "
                    f"{report.get('completed', 0)}/{report.get('total_questions', 0)} 题，"
                    f"准确率 {report.get('accuracy', 0):.0f}%，"
                    f"幻觉率 {report.get('hallucination_rate', 0):.0f}%",
                    gr.update(value=rows)
                )
            except Exception as e:
                return f"❌ 评测异常: {e}", gr.update(value=[])

        el_eval_btn.click(
            fn=handle_eval,
            inputs=[el_eval_file, st_token],
            outputs=[el_eval_msg, el_eval_table])

        def render_report_list(token):
            if not token:
                return []
            r = do_eval_reports(token)
            if "error" in r:
                return []
            reports = r.get("data", {}).get("reports", [])
            rows = []
            for report in reports:
                rows.append([
                    report.get("id", ""),
                    report.get("testset_name", "")[:30],
                    report.get("total_questions", 0),
                    f"{report.get('accuracy', 0):.0f}%",
                    f"{report.get('hallucination_rate', 0):.0f}%",
                    str(report.get("created_at", ""))[:19],
                ])
            return rows

        el_report_refresh.click(
            fn=lambda t: render_report_list(t),
            inputs=[st_token],
            outputs=[el_report_list])

        def render_report_detail(token, report_id):
            if not token:
                return "请先登录"
            if not report_id or report_id <= 0:
                return "请输入有效的报告ID"
            r = do_eval_report_detail(token, int(report_id))
            if "error" in r:
                return f"获取失败: {r['error']}"
            data = r.get("data", {})
            if not data:
                return "报告不存在"
            detail = []
            detail.append(f"测试集名称: {data.get('testset_name', '')}")
            detail.append(f"总题数: {data.get('total_questions', 0)}")
            detail.append(f"完成数: {data.get('completed', 0)}")
            detail.append(f"准确率: {data.get('accuracy', 0):.2f}%")
            detail.append(f"幻觉率: {data.get('hallucination_rate', 0):.2f}%")
            detail.append(f"平均匹配分: {data.get('avg_match_score', 0):.2f}")
            detail.append(f"创建时间: {data.get('created_at', '')}")
            detail.append("\n--- 逐题详情 ---")
            for item in data.get("results", []):
                detail.append(f"\n题号 {item.get('question_id', '')}:")
                detail.append(f"  问题: {item.get('question', '')}")
                detail.append(f"  匹配分: {item.get('match_score', 0):.2f}")
                detail.append(f"  幻觉: {'是' if item.get('is_hallucination') else '否'}")
                if item.get('hallucination_detail'):
                    detail.append(f"  详情: {item.get('hallucination_detail')}")
            return "\n".join(detail)

        el_report_detail_btn.click(
            fn=render_report_detail,
            inputs=[st_token, el_report_id],
            outputs=[el_report_detail])

        el_report_refresh.click(
            fn=lambda t: render_report_list(t),
            inputs=[st_token],
            outputs=[el_report_list])

    return app