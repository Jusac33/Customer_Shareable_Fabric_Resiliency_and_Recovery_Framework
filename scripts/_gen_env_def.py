import base64, json

SPARK_YML = """enable_native_execution_engine: false
driver_cores: 4
driver_memory: 28g
executor_cores: 4
executor_memory: 28g
dynamic_executor_allocation:
  enabled: true
  min_executors: 1
  max_executors: 2
spark_conf:
  spark.executorEnv.BCDR_ENV: production
  spark.executorEnv.BCDR_REGION: eastus
  spark.executorEnv.BCDR_APP_NAME: CrestShield
  spark.driverEnv.BCDR_ENV: production
  spark.driverEnv.BCDR_REGION: eastus
  spark.driverEnv.BCDR_APP_NAME: CrestShield
runtime_version: "1.3"
"""

ENV_YML = """dependencies:
  - numpy==1.26.4
  - pip:
      - requests==2.31.0
"""

definition = {
    "definition": {
        "parts": [
            {"path": "Setting/Sparkcompute.yml", "payload": base64.b64encode(SPARK_YML.encode()).decode(), "payloadType": "InlineBase64"},
            {"path": "Libraries/PublicLibraries/environment.yml", "payload": base64.b64encode(ENV_YML.encode()).decode(), "payloadType": "InlineBase64"},
        ]
    }
}

with open("scripts/_env_def.json", "w") as f:
    json.dump(definition, f)
print("Wrote scripts/_env_def.json")
print(json.dumps(definition, indent=2))
