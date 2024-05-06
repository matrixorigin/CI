package main

import (
	"fmt"
	"time"

	"github.com/guguducken/upload-artifact-oss/pkg/config"
	"github.com/guguducken/upload-artifact-oss/pkg/github"
	"github.com/guguducken/upload-artifact-oss/pkg/oss"
)

func main() {
	// get critical data
	provider := config.GetOSSProvider()
	accseeKey := config.GetOSSAccessKey()
	secretKey := config.GetOSSSecretKey()
	region := config.GetOSSRegion()
	bucket := config.GetOSSBucket()
	ossStorePath := config.GetOSSStorePath()
	storageClass := config.GetOSSStorageClass()

	artifactPath := config.GetArtifactPath()

	// get optional data
	retryTimes := config.GetRetryTimes()

	// generate oss client
	var client oss.Client
	switch provider {
	case oss.CosProvider:
		client = oss.NewCOS(accseeKey, secretKey)
	default:
		panic("unknown oss provider")
	}

	// validate client
	err := client.Validate(bucket, region)
	if err != nil {
		panic("failed to validate oss provider: " + err.Error())
	}

	// start upload artifact to oss
	for i := 0; i < retryTimes; i++ {
		github.Info(fmt.Sprintf("start upload oss artifact upload at try %d", i))
		err = client.UploadArtifact(bucket, region, storageClass, ossStorePath, artifactPath)
		if err != nil {
			fmt.Printf("upload artifact to oss try %s failed: %s, will sleep 10 seconds and retry\n", i, err.Error())
			time.Sleep(10 * time.Second)
			continue
		}
		github.Info(fmt.Sprintf("upload artifact to oss try %d successfully", i))
		break
	}
}
