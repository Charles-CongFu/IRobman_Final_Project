from src.grasping.grasp_generation import GraspGeneration
import numpy as np
import pybullet as p  # Import pybullet for visualization

class GraspExecution:
    """Robot grasping execution class, responsible for planning and executing complete grasping actions"""
    
    def __init__(self, sim):
        """
        Initialize grasping executor
        
        Parameters:
            sim: Simulation environment object
        """
        self.sim = sim
        from src.ik_solver import DifferentialIKSolver
        self.ik_solver = DifferentialIKSolver(sim.robot.id, sim.robot.ee_idx, damping=0.05)
        from src.path_planning.simple_planning import SimpleTrajectoryPlanner
        self.trajectory_planner = SimpleTrajectoryPlanner
    
    def compute_grasp_poses(self, best_grasp):
        """
        Calculate pre-grasp and final grasp poses based on the best grasp
        
        Parameters:
            best_grasp: Best grasp pose (R, grasp_center)
            
        Returns:
            tuple: (pose1_pos, pose1_orn, pose2_pos, pose2_orn)
        """
        R, grasp_center = best_grasp
        
        # Build offset vector in gripper coordinate system
        local_offset = np.array([0, 0.06, 0])
        
        # Transform offset vector from gripper coordinate system to world coordinate system
        world_offset = R @ local_offset
        
        # Calculate compensated end-effector target position
        ee_target_pos = grasp_center + world_offset
        
        # Add coordinate system transformation
        combined_transform = np.array([
            [0, -1, 0],
            [0, 0, -1],
            [1, 0, 0]
        ])
        
        # Apply combined transformation
        R_world = R @ combined_transform
        
        # Convert rotation matrix to quaternion
        from scipy.spatial.transform import Rotation
        rot_world = Rotation.from_matrix(R_world)
        euler_world = rot_world.as_euler('xyz', degrees=True)
        
        # Define pose 2 (final grasp pose)
        pose2_pos = ee_target_pos
        pose2_orn = p.getQuaternionFromEuler([euler_world[0]/180*np.pi, euler_world[1]/180*np.pi, euler_world[2]/180*np.pi])
        
        # Calculate pose 1 (pre-grasp position) - move along z-axis of pose 2 backwards
        pose2_rot_matrix = R_world
        z_axis = pose2_rot_matrix[:, 2]
        pose1_pos = pose2_pos - 0.15 * z_axis
        pose1_orn = pose2_orn
        
        return pose1_pos, pose1_orn, pose2_pos, pose2_orn
    
    def execute_grasp(self, best_grasp, grasp_poses=None):
        """
        Execute complete grasping process
        
        参数：
            best_grasp: 最佳抓取姿态 (R, grasp_center)
            grasp_poses: 可选的预计算姿态 (pose1_pos, pose1_orn, pose2_pos, pose2_orn)
            
        Returns:
            bool: True if grasping is successful, False otherwise
        """
        # 获取抓取姿态
        if grasp_poses is None:
            grasp_poses = self.compute_grasp_poses(best_grasp)
        
        pose1_pos, pose1_orn, pose2_pos, pose2_orn = grasp_poses
        
        # 获取当前机器人关节角度
        start_joints = self.sim.robot.get_joint_positions()
        
        # Solve IK for pre-grasp position
        target_joints = self.ik_solver.solve(pose1_pos, pose1_orn, start_joints, max_iters=50, tolerance=0.001)
        
        if target_joints is None:
            print("IK cannot be solved, cannot move to pre-grasp position")
            return False
        
        # Generate and execute trajectory to pre-grasp position
        trajectory = self.trajectory_planner.generate_joint_trajectory(start_joints, target_joints, steps=100)
        self._execute_trajectory(trajectory)
        
        # Open gripper
        self.open_gripper()
        
        # Move to final grasp position
        current_joints = self.sim.robot.get_joint_positions()
        pose2_trajectory = self.trajectory_planner.generate_cartesian_trajectory(
            self.sim.robot.id, 
            self.sim.robot.arm_idx, 
            self.sim.robot.ee_idx,
            current_joints, 
            pose2_pos, 
            pose2_orn, 
            steps=50
        )
        
        if not pose2_trajectory:
            print("Cannot generate trajectory to final grasp position")
            return False
        
        self._execute_trajectory(pose2_trajectory)
        
        # Wait for stabilization
        self._wait(0.5)
        
        # Close gripper to grasp object
        self.close_gripper()
        
        # Lift object
        success = self.lift_object()
        
        return success
    
    def _execute_trajectory(self, trajectory, speed=1/240.0):
        """Execute trajectory"""
        for joint_target in trajectory:
            self.sim.robot.position_control(joint_target)
            for _ in range(1):
                self.sim.step()
                import time
                time.sleep(speed)
    
    def _wait(self, seconds):
        """Wait for specified seconds"""
        import time
        steps = int(seconds * 240)
        for _ in range(steps):
            self.sim.step()
            time.sleep(1/240.)
    
    def open_gripper(self, width=0.04):
        """Open robot gripper"""
        p.setJointMotorControlArray(
            self.sim.robot.id,
            jointIndices=self.sim.robot.gripper_idx,
            controlMode=p.POSITION_CONTROL,
            targetPositions=[width, width]
        )
        self._wait(0.5)
    
    def close_gripper(self, width=0.005):
        """Close robot gripper to grasp object"""
        p.setJointMotorControlArray(
            self.sim.robot.id,
            jointIndices=self.sim.robot.gripper_idx,
            controlMode=p.POSITION_CONTROL,
            targetPositions=[width, width]
        )
        self._wait(1.0)
    
    def lift_object(self, height=0.5):
        """Grasp object and lift it to specified height"""
        # Get current end-effector position and orientation
        current_ee_pos, current_ee_orn = self.sim.robot.get_ee_pose()
        
        # Calculate lifted position
        lift_pos = current_ee_pos.copy()
        lift_pos[2] += height
        
        # Get current joint angles
        current_joints = self.sim.robot.get_joint_positions()
        
        # Solve IK for lifted position
        lift_target_joints = self.ik_solver.solve(lift_pos, current_ee_orn, current_joints, max_iters=50, tolerance=0.001)
        
        if lift_target_joints is None:
            print("IK cannot be solved for lifted position, cannot lift object")
            return False
        
        # Generate and execute lifting trajectory
        lift_trajectory = self.trajectory_planner.generate_joint_trajectory(current_joints, lift_target_joints, steps=100)
        
        if not lift_trajectory:
            print("Cannot generate lifting trajectory")
            return False
        
        self._execute_trajectory(lift_trajectory, speed=1/240.0)
        return True

    def execute_complete_grasp(self, bbox, point_clouds, visualize=True):
        """
        Execute complete process of grasping planning and execution
        
        Parameters:
        bbox: Boundary box object
        point_clouds: Collected point cloud data
        visualize: Whether to visualize grasping process
        
        Returns:
        success: True if grasping is successful, False otherwise
        self: Grasping executor object (if grasping is successful)
        """
        import open3d as o3d
        from src.grasping import grasping_mesh
        from src.point_cloud.object_mesh import visualize_3d_objs
        
        print("\nStep 3: Grasping planning and execution...")
        
        # Merge point clouds
        print("\nPreparing to merge point clouds...")
        merged_pcd = None
        for data in point_clouds:
            if 'point_cloud' in data and data['point_cloud'] is not None:
                if merged_pcd is None:
                    merged_pcd = data['point_cloud']
                else:
                    merged_pcd += data['point_cloud']
        
        if merged_pcd is None:
            print("Error: Cannot merge point clouds, grasping terminated")
            return False, None
        
        # Get boundary box information
        center = bbox.get_center()
        rotation_matrix = bbox.get_rotation_matrix()
        min_point, max_point = bbox.get_aabb()
        obb_corners = bbox.get_corners()
        
        # Get rotated boundary box coordinates
        points_rotated = np.dot(np.asarray(merged_pcd.points) - center, rotation_matrix)
        min_point_rotated = np.min(points_rotated, axis=0)
        max_point_rotated = np.max(points_rotated, axis=0)
        
        print(f"\nBoundary box information:")
        print(f"Centroid coordinates: {center}")
        print(f"Minimum point in rotated coordinate system: {min_point_rotated}")
        print(f"Maximum point in rotated coordinate system: {max_point_rotated}")
        
        grasp_generator = GraspGeneration()
        
        # Generate grasping candidates
        print("\nGenerating grasping candidates...")
        sampled_grasps = grasp_generator.sample_grasps(
            center, 
            num_grasps=100, 
            sim=self.sim,
            rotation_matrix=rotation_matrix,
            min_point_rotated=min_point_rotated,
            max_point_rotated=max_point_rotated,
            center_rotated=center
        )
        
        # Create mesh for each grasping candidate
        all_grasp_meshes = []
        for grasp in sampled_grasps:
            R, grasp_center = grasp
            all_grasp_meshes.append(grasping_mesh.create_grasp_mesh(center_point=grasp_center, rotation_matrix=R))
        
        # Evaluate grasping quality
        print("\nEvaluating grasping quality...")
        
        best_grasp = None
        best_grasp_mesh = None
        highest_quality = 0
        
        for (pose, grasp_mesh) in zip(sampled_grasps, all_grasp_meshes):
            if not grasp_generator.check_grasp_collision(grasp_mesh, object_pcd=merged_pcd, num_colisions=1):
                R, grasp_center = pose
                
                valid_grasp, grasp_quality, _ = grasp_generator.check_grasp_containment(
                    grasp_mesh[0].get_center(), 
                    grasp_mesh[1].get_center(),
                    finger_length=0.05,
                    object_pcd=merged_pcd,
                    num_rays=50,
                    rotation_matrix=pose[0],
                    visualize_rays=False
                )
                
                if valid_grasp and grasp_quality > highest_quality:
                    highest_quality = grasp_quality
                    best_grasp = pose
                    best_grasp_mesh = grasp_mesh
                    print(f"Found better grasp, quality: {grasp_quality:.3f}")
        
        if best_grasp is None:
            print("No valid grasp found!")
            return False, None
        
        print(f"\nFound best grasp, quality score: {highest_quality:.4f}")
        
        # Calculate grasping pose (only calculate once)
        grasp_poses = self.compute_grasp_poses(best_grasp)
        pose1_pos, pose1_orn, pose2_pos, pose2_orn = grasp_poses

        # Visualize grasping pose
        if visualize:
            grasp_generator.visualize_grasp_poses(
                pose1_pos, pose1_orn, pose2_pos, pose2_orn, axis_length=0.1
            )
        
        # Execute grasping (pass calculated pose)
        print("\nStarting to execute grasping...")
        success = self.execute_grasp(best_grasp, grasp_poses)
        
        if success:
            print("\nGrasping successful!")
        else:
            print("\nGrasping failed...")
        
        # Add visualization code after finding the best grasp
        if best_grasp is not None and visualize:
            # Create triangle mesh from point cloud
            obj_triangle_mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_alpha_shape(
                pcd=merged_pcd, 
                alpha=0.08
            )
            
            # Prepare list of meshes for visualization
            vis_meshes = [obj_triangle_mesh]
            
            # Add best grasp mesh to list
            vis_meshes.extend(best_grasp_mesh)
            
            # Call visualization function
            visualize_3d_objs(vis_meshes)
        
        return success, self if success else None