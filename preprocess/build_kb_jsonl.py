"""
Claude 离线预处理脚本
用途：利用 Claude Code 批量生成个人知识库 QA 对 + Agent 系统提示词 + 评测数据集

使用方式（在 Claude Code 中）：
    请读取以下文档，为每份文档生成 10-20 个面试常见问答对。
    输出格式为 JSONL，每行一个 JSON:
    {"id": "q001", "question": "...", "answer": "...", "source": "简历.pdf", "category": "个人信息"}

也可以生成评测测试集:
    {"id": "t001", "question": "...", "expected_answer": "...", "category": "技术能力"}

直接在 Claude Code 对话中执行此任务，无需运行此脚本。
"""

import json
from pathlib import Path


def generate_kb_qa_pairs(docs_dir: str, output_path: str):
    """
    从文档目录读取所有文件，拼接为 prompt，引导 Claude 生成 QA 对。

    注意: 此函数需要在 Claude Code 环境中运行，
    实际的知识库 QA 生成由 Claude 在对话中完成。
    此脚本提供数据格式和工具函数。
    """
    from rag.document_loader import load_documents_from_paths

    docs_dir = Path(docs_dir)
    supported = [".pdf", ".docx", ".txt", ".md"]
    filepaths = []

    for ext in supported:
        filepaths.extend([str(p) for p in docs_dir.glob(f"*{ext}")])
        filepaths.extend([str(p) for p in docs_dir.glob(f"*{ext.upper()}")])

    if not filepaths:
        print(f"在 {docs_dir} 中未找到支持的文档文件")
        return

    print(f"找到 {len(filepaths)} 个文档文件")

    # 加载所有文档
    docs = load_documents_from_paths(filepaths)
    all_content = "\n\n---\n\n".join(
        f"### 文件: {d['filename']}\n{d['content'][:5000]}" for d in docs
    )

    # 生成 prompt
    prompt = f"""
请基于以下个人资料文档，生成一套知识库 QA 问答对。

## 文档内容
{all_content[:30000]}

## 任务要求
1. 为每份文档生成 10-20 个面试常见问题及详细回答
2. 覆盖维度：个人背景、教育经历、项目经验、技术能力、工作经历
3. 回答必须严格基于原文，禁止编造
4. 以第一人称「我」来书写回答
5. 分类标签: 个人信息 / 技术能力 / 项目经验 / 教育背景

## 输出格式 (JSONL)
每行一个 JSON:
{{"id": "q001", "question": "...", "answer": "...", "source": "文件名", "category": "分类"}}
"""
    print("=" * 60)
    print("请将以下 prompt 复制到 Claude Code 中执行:")
    print("=" * 60)
    print(prompt)

    # 保存 prompt 供参考
    prompt_path = Path(output_path).parent / "kb_generation_prompt.txt"
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    print(f"\nPrompt 已保存到: {prompt_path}")


def generate_eval_testset(kb_jsonl_path: str, output_path: str):
    """
    从知识库 QA 对中采样，生成评测测试集 JSON。

    评测测试集格式:
    {{
        "name": "面试模拟评测集",
        "questions": [
            {{
                "id": "t001",
                "question": "...",
                "expected_answer": "...（可为空）",
                "category": "技术能力"
            }}
        ]
    }}
    """
    qa_pairs = []
    with open(kb_jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    qa_pairs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not qa_pairs:
        print("未找到有效的 QA 对")
        return

    # 采样（取每个分类的若干条）
    test_questions = []
    for qa in qa_pairs:
        test_questions.append(
            {
                "id": qa.get("id", f"t{len(test_questions) + 1:03d}"),
                "question": qa["question"],
                "expected_answer": qa.get("answer", ""),
                "category": qa.get("category", "未分类"),
            }
        )

    testset = {
        "name": "知识库问答评测集",
        "questions": test_questions,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(testset, f, ensure_ascii=False, indent=2)

    print(f"评测集已生成: {output_path} ({len(test_questions)} 题)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Claude 离线预处理工具")
    parser.add_argument("--mode", choices=["gen_prompt", "gen_testset"], default="gen_prompt")
    parser.add_argument("--docs_dir", default="./assets/upload_docs", help="文档目录")
    parser.add_argument("--kb_jsonl", default="./assets/test_data/knowledge_qa.jsonl")
    parser.add_argument("--output", default="./assets/test_data/eval_testset.json")

    args = parser.parse_args()

    if args.mode == "gen_prompt":
        generate_kb_qa_pairs(args.docs_dir, args.output)
    elif args.mode == "gen_testset":
        generate_eval_testset(args.kb_jsonl, args.output)
