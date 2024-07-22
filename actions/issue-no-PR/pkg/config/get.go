package config

import (
	"fmt"
	"issue-no_pull-request-action/pkg/github"
	"strconv"
)

func GetBaseURL() string {
	return github.MustGetInput(EnvBaseURL)
}

func GetOwner() string {
	return github.MustGetInput(EnvOwner)
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

func GetAssignees() string {
	return github.MustGetInput(EnvAssignees)
}

func GetLabels() string {
	return github.MustGetInput(EnvLabels)
}