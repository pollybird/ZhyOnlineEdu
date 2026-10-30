"""通用 sys_task worker（§12.3：导入/订单补偿/归档/进度对账）。

compose: entrypoint ["python","-m","zhyonlineedu.workers.celery"]
注意：progress-flush 不走本 worker（§12.2：单写者语义，避免双 worker 同 flush 同一 gw_seq 区间）。
"""


def main() -> None:
    raise NotImplementedError("M2/M3：TaskQueue 抽象（默认 DB-backed，§R8）")


if __name__ == "__main__":
    main()
