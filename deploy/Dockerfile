# 股票看盤系統 — NAS(Synology DS218+,Intel x86-64)部署映像
# 只裝「執行環境+相依套件」;程式碼與資料(DB/model/設定)用 bind-mount 掛進 /app,
# 所以在 NAS 上改檔或更新資料庫都不用重 build image。
#
# 必須用 3.14(對齊開發機):ml_backend 的模型環境閘門(compare_model_environment)硬性要求
# python major.minor 與 model_env.json 記錄的一致(3.14),否則 model.pkl 直接被判「不可載入」、
# 妖股判斷全停擺。numpy 2.4.4/scipy/sklearn/xgboost/lightgbm/shioaji 1.3.3 都有 cp314 manylinux
# x86_64 wheel(免編譯),所以 3.14 裝得起來。
FROM python:3.14-slim

# libgomp1 = lightgbm/xgboost 需要的 OpenMP runtime;tzdata = 排程算台北時間(zoneinfo)要用
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Taipei \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先只複製 requirements 裝套件(這層會被 Docker 快取,之後改程式不會重裝一次套件)
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 程式碼/資料不 COPY 進 image,由 docker-compose 的 bind-mount 掛進 /app
EXPOSE 8008
CMD ["python", "server.py"]
