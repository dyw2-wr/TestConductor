"""Django settings for TestConductor."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured


# 采用北京时间
TIME_ZONE = 'Asia/Shanghai'
USE_TZ = True

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# 本地开发可以使用项目根目录的 .env；生产环境应由进程管理器注入变量。
load_dotenv(BASE_DIR / ".env")

# 确保项目根目录在Python路径中
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be configured")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv("DJANGO_DEBUG", "false").strip().lower() in {"1", "true", "yes"}

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

# 添加上传测试用例文件目录配置
MEDIA_ROOT = os.path.join(BASE_DIR, 'uploads')
MEDIA_URL = '/uploads/'

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'apps.test_platform.apps.TestPlatformConfig',
]

# Every persisted report path is relative to this logical storage root.
_artifact_root = Path(os.getenv("TEST_PLATFORM_ARTIFACT_ROOT", "run_artifacts"))
if not _artifact_root.is_absolute():
    _artifact_root = BASE_DIR / _artifact_root
TEST_PLATFORM_ARTIFACT_ROOT = _artifact_root.resolve()

# v4 model boundary. Keep credentials in process environment, never in Django
# models or checked-in configuration.
TEST_PLATFORM_LLM_API_KEY = os.getenv("TEST_PLATFORM_LLM_API_KEY", "")
TEST_PLATFORM_LLM_BASE_URL = os.getenv("TEST_PLATFORM_LLM_BASE_URL", "")
TEST_PLATFORM_LLM_MODEL = os.getenv("TEST_PLATFORM_LLM_MODEL", "")
# Optional task-specific overrides. Empty values keep the existing single-model
# setup fully compatible.
TEST_PLATFORM_DESIGN_LLM_MODEL = os.getenv("TEST_PLATFORM_DESIGN_LLM_MODEL", "")
TEST_PLATFORM_PLANNING_LLM_MODEL = os.getenv("TEST_PLATFORM_PLANNING_LLM_MODEL", "")
TEST_PLATFORM_LLM_TIMEOUT_SECONDS = float(
    os.getenv("TEST_PLATFORM_LLM_TIMEOUT_SECONDS", "120")
)
TEST_PLATFORM_LLM_CONNECT_TIMEOUT_SECONDS = float(
    os.getenv("TEST_PLATFORM_LLM_CONNECT_TIMEOUT_SECONDS", "10")
)
# The OpenAI SDK retries transient failures by default.  Model-backed planning
# already has one bounded semantic repair, so hidden transport retries make a
# slow provider look like a hung application.  Keep the retry budget explicit.
TEST_PLATFORM_LLM_MAX_RETRIES = int(os.getenv("TEST_PLATFORM_LLM_MAX_RETRIES", "0"))
# Background generators are separate processes; this file-backed gate limits
# their combined provider pressure on a single application host.
TEST_PLATFORM_LLM_MAX_CONCURRENT_CALLS = int(
    os.getenv("TEST_PLATFORM_LLM_MAX_CONCURRENT_CALLS", "2")
)
TEST_PLATFORM_LLM_QUEUE_TIMEOUT_SECONDS = float(
    os.getenv("TEST_PLATFORM_LLM_QUEUE_TIMEOUT_SECONDS", "15")
)
# Optional planning-only Milvus retrieval. The authoritative approved content
# remains in TEST_PLATFORM_APPROVED_KNOWLEDGE_CATALOG and is revalidated after
# every vector hit.
TEST_PLATFORM_MILVUS_ENABLED = os.getenv(
    "TEST_PLATFORM_MILVUS_ENABLED", "false"
).strip().lower() in {"1", "true", "yes"}
TEST_PLATFORM_MILVUS_URI = os.getenv(
    "TEST_PLATFORM_MILVUS_URI", "http://localhost:19530"
)
TEST_PLATFORM_MILVUS_TOKEN = os.getenv("TEST_PLATFORM_MILVUS_TOKEN", "")
TEST_PLATFORM_MILVUS_DATABASE = os.getenv("TEST_PLATFORM_MILVUS_DATABASE", "default")
TEST_PLATFORM_MILVUS_COLLECTION = os.getenv(
    "TEST_PLATFORM_MILVUS_COLLECTION", "test_conductor_knowledge_v1"
)
TEST_PLATFORM_MILVUS_DENSE_WEIGHT = float(
    os.getenv("TEST_PLATFORM_MILVUS_DENSE_WEIGHT", "0.65")
)
TEST_PLATFORM_MILVUS_SPARSE_WEIGHT = float(
    os.getenv("TEST_PLATFORM_MILVUS_SPARSE_WEIGHT", "0.35")
)
TEST_PLATFORM_EMBEDDING_DEVICE = os.getenv("TEST_PLATFORM_EMBEDDING_DEVICE", "cpu")
TEST_PLATFORM_EMBEDDING_PROVIDER = os.getenv(
    "TEST_PLATFORM_EMBEDDING_PROVIDER", "hashing"
)
TEST_PLATFORM_APPROVED_KNOWLEDGE_CATALOG = os.getenv(
    "TEST_PLATFORM_APPROVED_KNOWLEDGE_CATALOG", ""
)
# Deployments with secret stores or live transports should provide a dotted
# factory. JSON is retained as a local-development fallback and must not be
# committed when it contains credentials.
TEST_PLATFORM_RUNTIME_CONTEXT_FACTORY = os.getenv(
    "TEST_PLATFORM_RUNTIME_CONTEXT_FACTORY", ""
)
TEST_PLATFORM_RUNTIME_CONTEXT_JSON = os.getenv(
    "TEST_PLATFORM_RUNTIME_CONTEXT_JSON", ""
)

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'apps.test_platform.local_access.LocalOnlyMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database. 默认 SQLite 便于平台边界和测试启动；部署到 MySQL 时显式设置 DB_ENGINE。
DB_ENGINE = os.getenv("DB_ENGINE", "django.db.backends.sqlite3")
if DB_ENGINE.endswith("sqlite3"):
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINE,
            "NAME": os.getenv("DB_NAME", str(BASE_DIR / "db.sqlite3")),
            "OPTIONS": {
                # SQLite is for local/small deployments only, but waiting for a
                # short concurrent write is safer than immediately surfacing
                # "database is locked" from background model workers.
                "timeout": float(os.getenv("DB_SQLITE_TIMEOUT_SECONDS", "20")),
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINE,
            "NAME": os.getenv("DB_NAME", "test_conductor_db"),
            "USER": os.getenv("DB_USER", ""),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "3306"),
        }
    }

# Internationalization
LANGUAGE_CODE = 'zh-hans'
USE_I18N = True

# Static files are provided by installed Django applications (notably admin).
# Production deployments should run collectstatic and serve STATIC_ROOT via the
# fronting web server.
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / "staticfiles"

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
