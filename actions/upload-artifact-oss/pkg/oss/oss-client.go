package oss

const (
	CosProvider = "cos"
)

var SupportedProviders = []string{CosProvider}

type Client interface {
	SetAccessKey(accessKey string)
	SetSecretKey(secretKey string)

	Validate(bucket string, region string) error

	UploadArtifact(bucket string, region string, storageClass string, storePath string, artifactPath string) error
	HeadArtifact(bucket string, region string, ossPath string) error
}
