package config

import (
	"fmt"
	"os"
	"reopen-without-PR/pkg/github"
	"strconv"
)

func GetBaseURL() string {
	return os.Getenv("BASE_URL")
}

func GetRepository() string {
	return github.MustGetInput(EnvRepository)
}

func GetIssueNumber() int {
	issueNum := github.MustGetInput(EnvIssueNumber)
	if issueNum == "" {
		return 0
	}
	iNum, err := strconv.Atoi(issueNum)
	if err != nil {
		fmt.Printf("cannot parse issueNum: %s, will use default 0\n", iNum)
		return 0
	}
	return iNum
}

func GetGithubToken() string {
	return github.MustGetInput(EnvGithubToken)
}

func GetCurrentAssignee() string {
	return github.MustGetInput(EnvCurrentAssignee)
}

func GetLabels() string {
	return github.MustGetInput(EnvLabels)
}

func GetBlackList() string {
	return github.MustGetInput(EnvBlacklist)
}
