package main

import (
	"fmt"
	"reopen-without-PR/pkg/config"
	ihttp "reopen-without-PR/pkg/http"
	"strings"
)

func main() {
	//设置GitHub API的基本URL和认证信息
	//Examples of parameters are as follows
	//baseURL := "https://api.github.com"
	//repo := "rosyrain/github_action_test"
	//issueNumber := 53
	//token := "xxxxxx"
	//labelData := `needs-triage,bug`

	baseURL := config.GetBaseURL()
	repo := config.GetRepository()
	issueNumber := config.GetIssueNumber() // 你要检查的issue编号
	token := config.GetGithubToken()       // 从环境变量中获取GitHub访问令牌
	labelData := config.GetLabels()

	fmt.Printf("rm-labels: %v\n", labelData)

	labelSlice := strings.Split(labelData, ",")

	// 删除标签
	success := true
	for _, label := range labelSlice {
		labelURL := fmt.Sprintf("%s/repos/%s/issues/%d/labels/%s", baseURL, repo, issueNumber, label)
		_, err := ihttp.Request("DELETE", labelURL, token, nil, "")
		if err != nil {
			success = false
			fmt.Println(fmt.Sprintf("Error delete label,err:%v", err))
		}
	}
	if !success {
		panic(fmt.Sprintf("delete labels failed"))
	}

	fmt.Printf("delete labels %s successfully.\n", labelData)

}
