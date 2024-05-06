# Get Changed Paths

This action adds a size label based on the number of lines of code modified by PR

### First

Create a workflow `labeler.yml` file in your repositories `.github/workflows `directory.

### Inputs

#### github-token

The GitHub Actions token. e.g. `secrets.TOKEN_ACTION`. For more information,See this link: [Creating a personal access token](https://docs.github.com/cn/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token). This token must have `write` permission to the repository.

#### ignore

The files what you want to ignore in this repository. Different paths must be separated by commas. For example: if you set ignore to `.md`, this action will ignore the change size of any md files.

### size
The labels correspond to different modification sizes. The default is:
~~~javascript
{
    "xs": 0,
    "s": 10,
    "m": 100,
    "l": 500,
    "xl": 1000,
    "xxl": 2000,
}
~~~
You can pass your own configuration by passing `sizes` in `labeler.yml`

## Examples

~~~yaml

name: Test CI
on:
  pull_request_target:
    types: [opened, synchronize,reopened]
    branches: [ "master" ]

jobs:
  test-name:
    runs-on: ubuntu-latest
    name: Auto Add Labels

    steps:
      - name: Auto Add Labels
        uses: guguducken/label-size-action@v0.0.1
        with:
          size_token: ${{ secrets.ACTION_TOKEN }}
          ignore: ".txt"
          sizes: >
            {
              "xs":0,
              "s":10,
              "m":100,
              "l":500,
              "xl":1000,
              "xxl":2000
            }

~~~

# License

The scripts and documentation in this project are released under the [MIT License](https://github.com/guguducken/label-size-action/blob/master/LICENSE)