package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"reopen-without-PR/module"
	"reopen-without-PR/pkg/config"
	"reopen-without-PR/pkg/github"
	ihttp "reopen-without-PR/pkg/http"
	"strings"
)

func main() {
	// 设置GitHub API的基本URL和认证信息
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

	// 1.获取原始标签
	labelNeedURL := fmt.Sprintf("%s/repos/%s/issues/%d/labels", baseURL, repo, issueNumber)
	labelNeedResp, err := ihttp.Request("GET", labelNeedURL, token, nil, "")
	if err != nil {
		panic(fmt.Sprintf("labelNeedUrl http.Request failed,err:%v", err))
	}
	if labelNeedResp.StatusCode != http.StatusOK {
		panic(fmt.Sprintf("connect labelNeedUrl failed,resp.statusCode:%d", labelNeedResp.StatusCode))
	}
	defer labelNeedResp.Body.Close()

	lbody, err := io.ReadAll(labelNeedResp.Body)
	if err != nil {
		panic(fmt.Sprintf("Error reading issue labels response body:%v", err))
	}

	var labels []module.Label
	err = json.Unmarshal(lbody, &labels)
	if err != nil {
		panic(fmt.Sprintf("Error unmarshalling JSON:%v", err))
	}
	fmt.Printf("old labels are:%v\n", labels)

	// 2.过滤要删除的标签
	rmMap := map[string]bool{}
	rmSlice := strings.Split(labelData, ",")
	for _, label := range rmSlice {
		rmMap[label] = true
	}

	i := 0
	for _, label := range labels {
		if !rmMap[label.Name] {
			labels[i] = label
			i += 1
		}
	}
	labels = labels[:i]
	fmt.Printf("new labels are:%v\n", labels)

	// 3.重新添加标签
	labelURL := fmt.Sprintf("%s/repos/%s/issues/%d/labels", baseURL, repo, issueNumber)
	jsonData, err := json.Marshal(labels)
	if err != nil {
		panic("json.Marshal labelSlice failed")
	}
	labelResp, err := ihttp.Request("PUT", labelURL, token, bytes.NewReader(jsonData), "application/json")
	if err != nil {
		panic(fmt.Sprintf("Error adding label,err:%v", err))
	}
	if labelResp.StatusCode != http.StatusOK {
		panic(fmt.Sprintf("labelURL http.Request failed,labelStatusCode:%d", labelResp.StatusCode))
	}
	defer labelResp.Body.Close()
	fmt.Printf("delete labels %s successfully.\n", labelData)

	if err := github.SetOutput("send", "yes"); err != nil {
		fmt.Println("set outputs failed")
	}
}
