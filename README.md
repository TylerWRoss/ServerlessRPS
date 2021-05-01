# ServerlessRPS

A Continuous-Deployment, Serverless Rock-Paper-Scissors-Over-SMS Reference Implementation Using Amazon Web Services


## Overview

This repository accompanies [the ServerlessRPS (SRPS) whitepaper](docs/whitepaper.pdf), and includes the CloudFormation template for the "toolchain" stack as well as the templates and source code for the "application" stack.

Specifically, `Toolchain.yaml` is the CloudFormation template for the AWS CodePipeline-based Toolchain Stack. `template.yml` is the AWS SAM template for the Lambda Application deployed by the pipeline, with `buildspec.yml` defining how the application is packaged for deployment. Finally, `serverless_rps/` contains the source code of the reference application (an implementation of Rock-Paper-Scissors, played over SMS).

This README, and the other documentation in `docs/`, focuses on describing the deployment, configuration, use, and decommissioning of the system. For a discussion of the architecture, please refer to the paper.



## Deployment

The deployment process (both toolchain, and SAM CLI-based) is documented in [docs/Deployment.md](docs/Deployment.md).



## Decommissioning

The decommissioning (teardown) process is documented in [docs/Decommission.md](docs/Decommission.md).



## Implementation Details

Implementation details of the application (code structure, databases, etc.) are documented in [docs/Application.md](docs/Application.md).



## Where to Start?

If you've deployed the application, and are looking for somewhere to start hacking, take a look at Section 3.1.2 in the [whitepaper](docs/whitepaper.pdf), "A Liveness Edge-case: Lambda Timeouts". The SRPS application neglects an edge-case in its (un)locking scheme that can result in stale locks if a Lambda Function instance is terminated prematurely (i.e., by timeout).



## Reusing the Toolchain

As discussed in the SRPS paper, the Toolchain is designed to be (reasonably) application agnostic. To use the Toolchain with another AWS SAM application, the `CloudFormationRole` (which constrains the deployment) and `PermissionsBoundaryPolicy` (which constrains the application itself) must be updated to reflect the services/resources of that application. As-is, the `CloudFormationRole` is limited to the resources and actions needed to deploy and run the SRPS application, and the `PermissionsBoundaryPolicy` restricts the application to prefix-named Lambda, SQS, and DynamoDB resources, and the Pinpoint 'SendMessages' action. Thus, to support another application, these IAM resources (in `Toolchain.yaml`) must be updated appropriately.

Assuming the IAM policies of the Toolchain are appropriate for the application, when deploying the Toolchain Stack, specify a GitHub repository containing an AWS SAM application. The application will be deployed by the Toolchain in the same manner as SRPS would be.

NOTE: The `buildspec.yml` included with SRPS (processed by AWS CodeBuild) produces `template-export.yml` which is then deployed by CloudFormation. The buildspec of any other application must also produce a `template-export.yml`, *or* the `TemplatePath` property of the CloudFormation Action defined on the ProjectPipeline Resource of `Toolchain.yaml` must be updated.
