from aws_cdk import (core as cdk, aws_ec2 as ec2, aws_ecs as ecs,
                    aws_elasticloadbalancingv2 as elb,
                     aws_ecs_patterns as ecs_patterns)


# For consistency with other languages, `cdk` is the preferred import name for
# the CDK's core module.  The following line also imports it as `core` for use
# with examples from the CDK Developer's Guide, which are in the process of
# being updated to use `cdk`.  You may delete this import if you don't need it.
from aws_cdk import core


class CdkMonStack(cdk.Stack):

    def __init__(self, scope: cdk.Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The code that defines your stack goes here

        # VPC
        vpc = ec2.Vpc(self, "VPC",
            max_azs=2,
            cidr="10.10.0.0/16",
            # configuration will create 3 groups in 2 AZs = 6 subnets.
            subnet_configuration=[ec2.SubnetConfiguration(
                subnet_type=ec2.SubnetType.PUBLIC,
                name="Public",
                cidr_mask=24
            ), ec2.SubnetConfiguration(
                subnet_type=ec2.SubnetType.PRIVATE,
                name="Private",
                cidr_mask=24
            ), ec2.SubnetConfiguration(
                subnet_type=ec2.SubnetType.ISOLATED,
                name="DB",
                cidr_mask=24
            )
            ],
            nat_gateways=2
            )
        self.vpc = vpc

        # security group
        sg = ec2.SecurityGroup(self, id = 'sg_int', vpc = vpc)
        self.sg = sg
        task = ecs.FargateTaskDefinition(self,
                                         id = 'MonitorTask',
                                         cpu = 512,
                                         memory_limit_mib = 2048
                                         #volumes = [ecs.Volume(name = cfgVolName)]
        )

        # Create NLB
        nlb = elb.NetworkLoadBalancer(self, "nlb",
                                      vpc=vpc,
                                      internet_facing=False,
                                      cross_zone_enabled=True,
                                      load_balancer_name="nlb")

        self.nlb = nlb
        # ECS Cluster
        cluster = ecs.Cluster(self, "Monitoring", vpc=vpc)
        c_prometheus = task.add_container('prometheus',
                                          essential=False,
                                          image=ecs.ContainerImage.from_registry('prom/prometheus'),
                                          port_mappings = [ecs.PortMapping(container_port=9090)]
        )

        c_pushgateway = task.add_container('pushgateway',
                                           essential=False,
                                          image=ecs.ContainerImage.from_registry('prom/pushgateway'),
                                          port_mappings = [ecs.PortMapping(container_port=9091)]
        )

        c_grafana = task.add_container('grafana',
                                       essential=True,
                                       image=ecs.ContainerImage.from_registry('prom/prometheus'),
                                       port_mappings = [ecs.PortMapping(container_port=3000)]
        )

        service = ecs.FargateService(self, "EMQXMonitoring",
                                     security_group = self.sg,
                                     cluster = cluster,
                                     task_definition = task,
                                     desired_count = 1
        )

        listenerGrafana = self.nlb.add_listener('grafana', port = 3000);
        listenerPrometheus = self.nlb.add_listener('prometheus', port = 9090);
        listenerPushGateway = self.nlb.add_listener('pushgateway', port = 9091);

        listenerGrafana.add_targets(id = 'grafana', port=3000, targets = [service.load_balancer_target(
            container_name="grafana",
            container_port=3000
        )])
        listenerPrometheus.add_targets(id = 'prometheus', port=9090, targets=[service.load_balancer_target(
            container_name="prometheus",
            container_port=9090
        )])

        listenerPushGateway.add_targets(id = 'pushgateway', port=9091, targets=[service.load_balancer_target(
            container_name="pushgateway",
            container_port=9091
        )]) ,


        self.mon_lb = self.nlb.load_balancer_dns_name

