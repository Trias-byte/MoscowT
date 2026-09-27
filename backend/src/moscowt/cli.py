import argparse
import json
import logging
from pathlib import Path

from .config import Settings
from .domain import HISTORY_END, moscow_origin
from .storage import SnapshotStore


def main():
    parser = argparse.ArgumentParser(description="MoscowT reproducible preparation and forecast commands")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--dataset-dir", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--source-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare-labels", "prepare-network", "prepare-fleet", "backtest"):
        sub.add_parser(name)
    train = sub.add_parser("train")
    train.add_argument("--origin", default="2025-11-01")
    train.add_argument("--expert", choices=["auto", "seasonal", "catboost"], default="auto")
    forecast = sub.add_parser("forecast")
    forecast.add_argument("--model-id", required=True)
    forecast.add_argument("--horizon", choices=["day", "month", "competition_61d"], default="competition_61d")
    submission = sub.add_parser("submission")
    submission.add_argument("--snapshot-id")
    submission.add_argument("--output", type=Path, default=Path("../artifacts/submission.csv"))
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("bootstrap")
    sub.add_parser("openapi")
    worker = sub.add_parser("worker")
    worker.add_argument("--channel", choices=["general", "model"], required=True)
    demo = sub.add_parser("demo-prepare")
    demo.add_argument("--weather-dir", type=Path)
    demo.add_argument("--schedule", type=Path)
    demo.add_argument("--quick", action="store_true")
    restore = sub.add_parser("restore")
    restore.add_argument("archive", type=Path)
    sub.add_parser("bundle")
    args = parser.parse_args()
    settings = Settings()
    settings = Settings(
        **{
            name: getattr(args, name)
            for name in ("state_dir", "dataset_dir", "data_root", "source_dir")
            if getattr(args, name) is not None
        }
    )
    if args.command == "restore":
        from .bundles import restore_bundle

        print(json.dumps(restore_bundle(args.archive, settings)))
        return
    store = SnapshotStore(settings.state_dir)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "worker":
        from .platform_worker import run

        run(settings, args.channel)
        return
    if args.command == "bundle":
        from .bundles import create_bundle

        print(json.dumps(create_bundle(settings, "portable")))
        return
    if args.command == "demo-prepare":
        from .demo import prepare_demo

        print(
            json.dumps(
                prepare_demo(settings, args.weather_dir, args.schedule, args.quick),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)
        return
    if args.command == "openapi":
        from .api import create_app

        print(
            json.dumps(
                create_app(settings.model_copy(update={"worker_enabled": False})).openapi(),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    from .exports import SubmissionService
    from .fleet import prepare_fleet
    from .forecasting import backtest, fit_model, issue_forecast
    from .pipelines import prepare_labels, prepare_network

    if args.command == "prepare-labels":
        result = prepare_labels(
            store, settings.source_dir if (settings.source_dir / "labels").exists() else settings.dataset_dir
        )
    elif args.command == "prepare-fleet":
        result = prepare_fleet(store, settings.dataset_dir)
    elif args.command == "prepare-network":
        network = prepare_network(store, settings.dataset_dir)
        result = {"networkSnapshotId": network["networkSnapshotId"], "patterns": len(network["patterns"])}
    elif args.command == "backtest":
        result = backtest(store, store.current()["historyId"])
    elif args.command == "train":
        expert = args.expert
        if expert == "auto":
            report = json.loads((store.root / "backtest.json").read_bytes())
            if report["historyId"] != store.current()["historyId"]:
                raise ValueError("Backtest report does not match current history")
            expert = report["selectedExpert"]
        result = fit_model(store, store.current()["historyId"], moscow_origin(args.origin), expert)
        result = {k: v for k, v in result.items() if k != "profile"}
    elif args.command == "forecast":
        result = issue_forecast(store, args.model_id, args.horizon)
    elif args.command == "submission":
        result = SubmissionService(store, settings.dataset_dir).write(
            args.snapshot_id or store.current()["snapshotId"], args.output
        )
    elif args.command == "bootstrap":
        history = prepare_labels(
            store, settings.source_dir if (settings.source_dir / "labels").exists() else settings.dataset_dir
        )
        prepare_fleet(store, settings.dataset_dir)
        prepare_network(store, settings.dataset_dir)
        report = backtest(store, history["id"])
        model = fit_model(store, history["id"], HISTORY_END, report["selectedExpert"])
        run = issue_forecast(store, model["id"])
        result = {
            "forecast": run,
            "submission": SubmissionService(store, settings.dataset_dir).write(
                store.current()["snapshotId"], store.root / "submission.csv"
            ),
            "holdout": report["holdout"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
