"""
기존 signals 데이터 일괄 RAG 인덱싱 스크립트.

실행: python -m worker.rag_indexer
      python -m worker.rag_indexer --days 180
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="signals 테이블 RAG 일괄 인덱싱")
    parser.add_argument("--days", type=int, default=180, help="최근 N일 데이터 인덱싱 (기본: 180)")
    args = parser.parse_args()

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not openai_key:
        logger.error("OPENAI_API_KEY가 .env에 설정되지 않았습니다.")
        sys.exit(1)

    try:
        import faiss  # noqa: F401
    except ImportError:
        logger.error("faiss-cpu 미설치. 아래 명령어 실행 후 재시도하세요:")
        logger.error("  pip install faiss-cpu")
        sys.exit(1)

    logger.info(f"RAG 인덱싱 시작 (최근 {args.days}일)")
    from worker.agents.tools.rag_tools import (
        bulk_index_existing_signals,
        bulk_index_agent_memory,
    )

    signal_count = bulk_index_existing_signals(days=args.days)
    memory_counts = bulk_index_agent_memory(days=args.days)

    logger.info(f"신호 인덱싱 완료: {signal_count}건")
    logger.info(f"에이전트 메모리 인덱싱 완료: {memory_counts}")


if __name__ == "__main__":
    main()
