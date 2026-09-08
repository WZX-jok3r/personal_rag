## project folder

personal_rag/
├── knowledge_base/          # 原始文档
├── processed_cache/
│   └── cache.json           # 解析缓存
├── qdrant_storage/          # Qdrant 数据（Docker 挂载）
├── test_dataset/
│   └── qa_test.jsonl        # 评测集（你整理的）
├── src/
│   ├── config.py            #  全局配置
│   ├── document_loader.py   #  多格式解析
│   ├── chunk_strategy.py    #  差异化分块
│   ├── vector_store.py      #  Qdrant + Embedding
│   ├── ingest.py            #  增量入库
│   ├── rag_pipeline.py      #  RAG 链路
│   ├── webui.py             #  Gradio 前端
│   └── eval_runner.py       #  评测脚本
├── .env                     # API Key 等敏感配置
├── .gitignore
├── docker-compose.yml
├── requirements.txt
└── README.md