package config

import (
	"fmt"
	"strconv"

	"github.com/guguducken/upload-artifact-oss/pkg/github"
)

func GetOSSProvider() string {
	return github.MustGetInput(EnvOSSProvider)
}

func GetOSSAccessKey() string {
	return github.MustGetInput(EnvOSSAccessKey)
}
func GetOSSSecretKey() string {
	return github.MustGetInput(EnvOSSSecretKey)
}

func GetOSSBucket() string {
	return github.MustGetInput(EnvOSSBucket)
}

func GetOSSRegion() string {
	return github.MustGetInput(EnvOSSRegion)
}

func GetOSSStorePath() string {
	return github.MustGetInput(EnvOSSStorePath)
}

func GetArtifactPath() string {
	return github.MustGetInput(EnvArtifactPath)
}

func GetRetryTimes() int {
	times := github.GetInput(EnvRetryTimes)
	if times == "" {
		return 1
	}
	t, err := strconv.Atoi(times)
	if err != nil {
		fmt.Printf("cannot parse retry times: %s, will use default 1\n", times)
		return 1
	}
	return t
}

func GetOSSStorageClass() string {
	return github.MustGetInput(EnvOSSStorageClass)
}
