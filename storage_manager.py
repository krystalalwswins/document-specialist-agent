import io
import boto3
from botocore.exceptions import ClientError
from config import MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET_NAME

class StorageManager:
    """管理 MinIO/S3 开源对象存储的文件上传、下载与生命周期"""
    def __init__(self):
        self.s3_client = boto3.client(
            's3',
            endpoint_url=MINIO_ENDPOINT,
            aws_access_key_id=MINIO_ACCESS_KEY,
            aws_secret_access_key=MINIO_SECRET_KEY,
            region_name='us-east-1'
        )
        self.bucket = MINIO_BUCKET_NAME
        self._ensure_bucket_exists()

    def _ensure_bucket_exists(self):
        """确保指定的 Bucket 存在"""
        try:
            self.s3_client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.s3_client.create_bucket(Bucket=self.bucket)
            print(f"📦 [Storage] 成功创建 Bucket: {self.bucket}")

    def upload_file_content(self, object_name: str, content: bytes) -> str:
        """上传二进制/文本内容到对象存储"""
        self.s3_client.put_object(
            Bucket=self.bucket,
            Key=object_name,
            Body=content
        )
        return self.generate_presigned_url(object_name)

    def download_file_content(self, object_name: str) -> bytes:
        """从对象存储读取文件内容"""
        response = self.s3_client.get_object(Bucket=self.bucket, Key=object_name)
        return response['Body'].read()

    def generate_presigned_url(self, object_name: str, expires_in: int = 3600) -> str:
        """生成外网安全的临时下载 URL"""
        return self.s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket, 'Key': object_name},
            ExpiresIn=expires_in
        )