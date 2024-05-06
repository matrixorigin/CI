package oss

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"path/filepath"
	"sync"

	"github.com/schollz/progressbar/v3"
	"github.com/tencentyun/cos-go-sdk-v5"
)

type COS struct {
	accessKey string
	secretKey string
}

func NewCOS(accessKey, secretKey string) Client {
	return &COS{accessKey, secretKey}
}

func (c *COS) SetAccessKey(accessKey string) {
	c.accessKey = accessKey
}

func (c *COS) SetSecretKey(secretKey string) {
	c.secretKey = secretKey
}

func (c *COS) Validate(bucket string, region string) error {
	return nil
}

func (c *COS) UploadArtifact(bucket string, region string, storageClass string, storePath string, artifactPath string) error {
	cosUrlTpl := "https://%s.cos.%s.myqcloud.com"

	endpoint := fmt.Sprintf(cosUrlTpl, bucket, region)

	key := filepath.Join(storePath, filepath.Base(artifactPath))

	u, _ := url.Parse(endpoint)
	b := &cos.BaseURL{BucketURL: u}
	client := cos.NewClient(b, &http.Client{
		Transport: &cos.AuthorizationTransport{
			SecretID:  c.accessKey,
			SecretKey: c.secretKey,
		},
	})

	opt := &cos.ObjectPutOptions{
		ObjectPutHeaderOptions: &cos.ObjectPutHeaderOptions{
			ContentType:      "text/html",
			XCosStorageClass: storageClass,
			Listener:         &progress{nil, sync.Once{}},
		},
		ACLHeaderOptions: &cos.ACLHeaderOptions{
			XCosACL: "default",
		},
	}
	_, err := client.Object.PutFromFile(context.Background(), key, artifactPath, opt)
	return err
}

func (c *COS) HeadArtifact(bucket string, region string, ossPath string) error {
	return nil
}

type progress struct {
	bar  *progressbar.ProgressBar
	once sync.Once
}

func (p *progress) ProgressChangedCallback(event *cos.ProgressEvent) {
	p.once.Do(func() {
		p.bar = progressbar.Default(event.TotalBytes)
	})
	p.bar.Set(int(event.ConsumedBytes))
}
