from aws_cdk import (core as cdk, aws_ec2 as ec2, aws_ecs as ecs,
                    aws_logs as aws_logs,
                    aws_elasticloadbalancingv2 as elb,
                    aws_efs as efs,
                     aws_ecs_patterns as ecs_patterns)


# For consistency with other languages, `cdk` is the preferred import name for
# the CDK's core module.  The following line also imports it as `core` for use
# with examples from the CDK Developer's Guide, which are in the process of
# being updated to use `cdk`.  You may delete this import if you don't need it.
from aws_cdk import core
from aws_cdk.aws_logs import LogRetention, RetentionDays


class CdkMonStack(cdk.Stack):

    def __init__(self, scope: cdk.Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The code that defines your stack goes here

        # VPC
        vpc = ec2.Vpc(self, "VPC",
            max_azs=1,
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
            )
            ],
            nat_gateways=1
            )
        self.vpc = vpc



        # security group
        sg = ec2.SecurityGroup(self, id = 'sg_int', vpc = vpc)
        self.sg = sg

        cluster = ecs.Cluster(self, "Monitoring", vpc=vpc)



        # setup_shared_efs(self):
        self.sg_efs_mt = ec2.SecurityGroup(self, "sg_efs_mt", vpc=self.vpc)
        self.sg_efs_mt.add_ingress_rule(peer=self.sg, connection=ec2.Port.tcp(2049))
        self.shared_efs = efs.FileSystem(self, id='shared-data', vpc=self.vpc,
                                         removal_policy=core.RemovalPolicy.DESTROY,
                                         lifecycle_policy=efs.LifecyclePolicy.AFTER_14_DAYS,
                                         performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
                                         security_group=self.sg
                                         )



        # self.ap_prom_data = efs.AccessPoint(self, "shared-data-prom",
        #                                     path='/',
        #                                     create_acl=efs.Acl(
        #                                         owner_uid="65534",
        #                                         owner_gid="65534",
        #                                         permissions="0777"
        #                                     ),
        #                                     posix_user=efs.PosixUser(
        #                                         uid="65534",  # nobody
        #                                         gid="65534"),
        #                                     file_system=self.shared_efs)

        self.ap_prom_data = efs.AccessPoint(self, "shared-data-prom",
                                            path='/tsdb_data',
                                            create_acl=efs.Acl(
                                                owner_uid="65534",
                                                owner_gid="65534",
                                                permissions="777"
                                            ),
                                            posix_user=efs.PosixUser(
                                                uid="65534",  # nobody
                                                gid="65534"),
                                            file_system=self.shared_efs)

        self.prom_data_vol = ecs.Volume(
                                name="prom_data",
                                efs_volume_configuration=ecs.EfsVolumeConfiguration(
                                    file_system_id=self.shared_efs.file_system_id,
                                    transit_encryption='ENABLED',
                                    authorization_config= ecs.AuthorizationConfig(access_point_id=self.ap_prom_data.access_point_id),
                                    #root_directory="/tsdb_data",
                            )
        )

        task = ecs.FargateTaskDefinition(self,
                                         id = 'MonitorTask',
                                         cpu = 512,
                                         memory_limit_mib = 2048,
                                         volumes = [self.prom_data_vol]
        )
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(9100), 'prometheus node exporter')
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(9091), 'prometheus pushgateway')
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(3000), 'grafana')
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(2049), 'NFS')
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.all_traffic(), 'all traffic')


        # Create NLB
        nlb = elb.NetworkLoadBalancer(self, "nlb",
                                      vpc=vpc,
                                      internet_facing=False,
                                      cross_zone_enabled=True,
                                      load_balancer_name="nlb")

        self.nlb = nlb



        # ECS Cluster
        with open("./user_data/prometheus.yml") as f:
                prometheus_config = f.read()

        task.add_volume(name = 'prom_config')
        c_config = task.add_container('config-prometheus',
                                       image=ecs.ContainerImage.from_registry('bash'),
                                       essential=False,
                                       logging = ecs.LogDriver.aws_logs(stream_prefix="mon_config_prometheus",
                                                                        log_retention = aws_logs.RetentionDays.ONE_DAY
                                       ),
                                       command = [ "-c",
                                                   "echo $DATA | base64 -d - | tee /tmp/private/prometheus.yml"
                                                 ],
                                       environment = {'DATA' : cdk.Fn.base64(prometheus_config)}

        )
        c_config.add_mount_points(ecs.MountPoint(read_only = False, container_path='/tmp/private', source_volume='prom_config'))
        c_prometheus = task.add_container('prometheus',
                                          essential=True,
                                          # https://github.com/prometheus/prometheus/blob/main/Dockerfile
                                          image=ecs.ContainerImage.from_registry('prom/prometheus'),
                                          port_mappings = [ecs.PortMapping(container_port=9090)],
                                          command = [
                                              "--config.file=/etc/prometheus/private/prometheus.yml",
                                              "--storage.tsdb.path=/prometheus/tsdb_data",
                                              "--web.console.libraries=/usr/share/prometheus/console_libraries",
                                              "--web.console.templates=/usr/share/prometheus/consoles"
                                          ],
                                          logging = ecs.LogDriver.aws_logs(stream_prefix="mon_prometheus",
                                                                        log_retention = aws_logs.RetentionDays.ONE_DAY
                                          ),

        )

        c_prometheus.add_mount_points(ecs.MountPoint(read_only = False, container_path='/etc/prometheus/private', source_volume='prom_config'))
        c_prometheus.add_mount_points(ecs.MountPoint(
             read_only=False, container_path='/prometheus/tsdb_data', source_volume=self.prom_data_vol.name))
        c_prometheus.add_container_dependencies(ecs.ContainerDependency(container=c_config, condition=ecs.ContainerDependencyCondition.COMPLETE))


        c_pushgateway = task.add_container('pushgateway',
                                           essential=False,
                                          image=ecs.ContainerImage.from_registry('prom/pushgateway'),
                                          #user='root',
                                          port_mappings = [ecs.PortMapping(container_port=9091)]
        )

        c_grafana = task.add_container('grafana',
                                       essential=True,
                                       image=ecs.ContainerImage.from_registry('grafana/grafana'),
                                       port_mappings = [ecs.PortMapping(container_port=3000)],
                                       logging = ecs.LogDriver.aws_logs(stream_prefix="mon_grafana",
                                                                        log_retention = aws_logs.RetentionDays.ONE_DAY)
        )

        service = ecs.FargateService(self, "EMQXMonitoring",
                                     security_group = self.sg,
                                     cluster = cluster,
                                     task_definition = task,
                                     desired_count = 1,
                                     #vpc_subnets = vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE),
                                     assign_public_ip = False

        )

        service.connections.allow_from(self.sg_efs_mt, ec2.Port.all_traffic(), "Allow EFS access")
        service.connections.allow_to(self.sg_efs_mt, ec2.Port.all_traffic(), "Allow EFS access")

        service.connections.allow_from(self.sg, ec2.Port.all_traffic(), "Allow EFS access")
        service.connections.allow_to(self.sg, ec2.Port.all_traffic(), "Allow EFS access")


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

