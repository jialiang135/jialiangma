"""
评测校验路由
"""
import json
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from loguru import logger

from core.schemas import APIResponse, EvalTestSet
from core.auth import get_current_user
from core.database import get_eval_reports, get_eval_report_by_id, insert_eval_report
from agent.eval_agent import eval_agent_node
from agent.state import AgentState

router = APIRouter(prefix="/api/eval", tags=["评测"])


@router.post("/testset/upload", response_model=APIResponse)
async def upload_testset(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """
    上传评测问答集 JSON 文件。
    格式: {"name": "测试集名称", "questions": [{"id": "1", "question": "...", "expected_answer": "..."}]}
    """
    if not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="仅支持 JSON 格式文件")

    try:
        content = await file.read()
        testset_data = json.loads(content.decode("utf-8"))

        # 校验格式
        EvalTestSet(**testset_data)

        # 保存到 test_data 目录
        from config.settings import settings
        test_dir = settings.resolve_path(settings.test_data_dir)
        import os
        save_path = os.path.join(str(test_dir), file.filename)
        with open(save_path, "wb") as f:
            f.write(content)

        logger.info(
            f"[Eval API] 测试集上传: {file.filename}, "
            f"{len(testset_data.get('questions', []))} 题"
        )

        return APIResponse(
            success=True,
            message=f"测试集上传成功: {testset_data.get('name', 'unnamed')}",
            data={"filename": file.filename, "question_count": len(testset_data.get("questions", []))},
        )

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON 格式错误")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=400, detail=f"测试集校验失败: {str(e)}")


@router.post("/run", response_model=APIResponse)
async def run_evaluation(
    testset_filename: str = None,
    user: dict = Depends(get_current_user),
):
    """
    运行自动评测。
    从已上传的测试集文件加载问题，批量执行问答 + 幻觉检测。
    """
    if not testset_filename:
        raise HTTPException(status_code=400, detail="请指定测试集文件名")

    from config.settings import settings
    test_dir = settings.resolve_path(settings.test_data_dir)
    import os
    filepath = os.path.join(str(test_dir), testset_filename)

    if not os.path.exists(filepath):
        # 尝试从 assets/test_data 直接加载
        from pathlib import Path
        alt_path = Path("assets/test_data") / testset_filename
        if alt_path.exists():
            filepath = str(alt_path)
        else:
            raise HTTPException(status_code=404, detail=f"测试集文件不存在: {testset_filename}")

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            testset = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取测试集失败: {str(e)}")

    logger.info(
        f"[Eval API] 开始评测: {testset_filename}, "
        f"{len(testset.get('questions', []))} 题"
    )

    # 构造 AgentState 并调用评测节点
    from langchain_core.messages import HumanMessage

    state: AgentState = {
        "messages": [HumanMessage(content="开始评测")],
        "owner_id": user["owner_id"],
        "username": user["username"],
        "agent_mode": "eval",
        "user_query": f"评测测试集: {testset.get('name', 'unnamed')}",
        "retrieved_docs": [],
        "knowledge_context": "",
        "reasoning_log": [],
        "tool_calls": [],
        "final_answer": "",
        "needs_tool_call": False,
        "available_tools": [],
        "iteration_count": 0,
        "error": None,
        "upload_files": None,
        "operation": None,
        "operation_result": None,
        "testset": testset,
        "eval_results": None,
        "eval_report": None,
    }

    result = await eval_agent_node(state)

    report = result.get("eval_report", {})
    if not report:
        return APIResponse(
            success=False,
            message="评测执行失败，未生成报告",
        )

    return APIResponse(
        success=True,
        message=f"评测完成: {report.get('testset_name', '')}",
        data=report,
    )


@router.get("/reports", response_model=APIResponse)
async def list_reports(user: dict = Depends(get_current_user)):
    """获取所有评测报告列表"""
    reports = get_eval_reports(user["owner_id"])

    summaries = [
        {
            "id": r["id"],
            "testset_name": r["testset_name"],
            "total_questions": r["total_questions"],
            "accuracy": r["accuracy"],
            "hallucination_count": r["hallucination_count"],
            "hallucination_rate": r["hallucination_rate"],
            "avg_match_score": r["avg_match_score"],
            "created_at": r["created_at"],
        }
        for r in reports
    ]

    return APIResponse(
        success=True,
        message=f"共 {len(reports)} 份评测报告",
        data={"reports": summaries},
    )


@router.get("/reports/{report_id}", response_model=APIResponse)
async def get_report_detail(
    report_id: int,
    user: dict = Depends(get_current_user),
):
    """获取指定评测报告的详细信息"""
    report = get_eval_report_by_id(report_id)

    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")

    return APIResponse(
        success=True,
        message=f"评测报告: {report['testset_name']}",
        data=report,
    )
