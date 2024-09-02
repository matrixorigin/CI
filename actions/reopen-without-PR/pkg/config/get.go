package config

import (
	"fmt"
	"reopen-without-PR/pkg/github"
	"strconv"
)

func GetBaseURL() string {
	return github.MustGetInput(EnvBaseURL)
}

func GetIssueOwner() string {
	return github.MustGetInput(EnvIssueOwner)
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

func GetAddLabels() string {
	return github.MustGetInput(EnvLabels)
}

func GetLabelsNeed() string {
	return github.MustGetInput(EnvLabelsNeed)
}

func GetWhiteList() string {
	return github.MustGetInput(EnvWhitelist)
}

func GetCloseUser() string {
	return github.MustGetInput(EnvCloseUser)
}
