```json
{
  // 启动全部时相邻服务之间的等待秒数
  "start_interval_seconds": 5,

  // 服务列表（顺序 = 启动顺序）
  "services": [
    {
      // 服务名称（必须唯一）
      "name": "InfluxDB",
      // 启动命令；第 0 个是可执行文件路径，后面是参数（此例无额外参数）
      "cmd": [
        "D:/IoT/guardian-data-center-deploy/guardian-data-center-deploy-V1.0/influxdb2-2.7.12-windows/influxd.exe"
      ],
      // 工作目录：程序需要在该目录下运行（相对路径资源依赖）
      "cwd": "D:/IoT/guardian-data-center-deploy/guardian-data-center-deploy-V1.0/influxdb2-2.7.12-windows",
      // 健康检查：等待端口 8086 监听，最多 60 秒
      "wait": { "type": "port", "value": 8086, "timeout": 60 },
      // 是否自动重启（false 不启用）
      "auto_restart": false,
      // 最大重启次数（auto_restart=false 时此值无效；此例写 3 也不会执行）
      "max_restarts": 3,
      // 第一次重启前等待（秒）
      "restart_backoff": 2,
      // 退避因子：第二次=2*1.5，第三次再乘 1.5 ...
      "restart_backoff_factor": 1.5,
      // 启动前必须存在的文件（为空表示不检查）
      "required_files": []
    },
    {
      "name": "Collector",
      "cmd": [
        "D:/IoT/guardian-data-center-deploy/guardian-data-center-deploy-V1.0/DataCenterV3.exe"
      ],
      "cwd": "D:/IoT/guardian-data-center-deploy/guardian-data-center-deploy-V1.0",
      // 监听端口 8088；若程序提前退出，会停止等待
      "wait": { "type": "port", "value": 8088, "timeout": 60 },
      "auto_restart": false,
      "max_restarts": 0,               // 0 表示不重启
      "restart_backoff": 2,
      "restart_backoff_factor": 1.5,
      // 该程序依赖的本地文件（缺失则不启动并标记失败）
      "required_files": ["config.xlsx", "sysconfig.json"]
    },
    {
      "name": "Grafana",
      "cmd": [
        "D:/IoT/grafana-12.0.2.windows-amd64/grafana-v12.0.2/bin/grafana-server.exe"
      ],
      "cwd": "D:/IoT/grafana-12.0.2.windows-amd64/grafana-v12.0.2/bin",
      "wait": { "type": "port", "value": 3000, "timeout": 60 },
      "auto_restart": false,
      "max_restarts": 0,
      "restart_backoff": 2,
      "restart_backoff_factor": 1.5,
      "required_files": []
    }
  ]
}
```
