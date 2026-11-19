import os
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer, AutoModelForSequenceClassification

# 模型目录已挪到 BettaFish_Modify 上一级，避免打进 Docker 构建上下文
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT_MODELS = os.path.join(os.path.dirname(_HERE), "models")
MODELS_ROOT = _PARENT_MODELS if os.path.isdir(os.path.dirname(_HERE)) else "./models"
os.makedirs(MODELS_ROOT, exist_ok=True)


def download_clustering_model():
    model_name = "paraphrase-multilingual-MiniLM-L12-v2"
    save_path = os.path.join(MODELS_ROOT, model_name)

    print(f"--- 开始下载聚类模型: {model_name} ---")
    if os.path.exists(save_path):
        print(f"目录已存在，跳过: {save_path}")
        return

    try:
        # 使用 SentenceTransformer 下载
        model = SentenceTransformer(model_name)
        model.save(save_path)
        print(f"✅ 聚类模型已保存到: {save_path}")
    except Exception as e:
        print(f"❌ 聚类模型下载失败: {e}")


def download_sentiment_model():
    model_name = "tabularisai/multilingual-sentiment-analysis"
    # 注意：sentiment_analyzer.py 中使用的是 transformers 库
    # 为了方便管理，我们将模型名中的 '/' 替换为 '_' 作为文件夹名
    folder_name = model_name.replace("/", "_")
    save_path = os.path.join(MODELS_ROOT, folder_name)

    print(f"\n--- 开始下载情感分析模型: {model_name} ---")
    if os.path.exists(save_path):
        print(f"目录已存在，跳过: {save_path}")
        return

    try:
        # 下载 tokenizer 和 model
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSequenceClassification.from_pretrained(model_name)

        # 保存到本地
        tokenizer.save_pretrained(save_path)
        model.save_pretrained(save_path)
        print(f"✅ 情感分析模型已保存到: {save_path}")
    except Exception as e:
        print(f"❌ 情感分析模型下载失败: {e}")


if __name__ == "__main__":
    download_clustering_model()
    download_sentiment_model()
    print(f"\n🎉 所有模型下载完成！请将 '{MODELS_ROOT}' 文件夹复制到离线环境的项目根目录。")