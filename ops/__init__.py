"""ops — reco(Mac Studio)运维域专用只读 fleet 监控控制台。

顶层 leaf 模块:只 import boto3/subprocess + stdlib,**不被** core/recorder/analytics
依赖,也不 import 它们的业务逻辑(探针在远端容器里复用 recorder_health,见 probe_aws)。
设计见 docs/design/RECO_OPS_GUI.md。
"""
